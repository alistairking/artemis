import argparse
import os
import time

import _pybgpstream
import redis
from kombu import Connection
from kombu import Exchange
from kombu import Producer
from netaddr import IPAddress
from netaddr import IPNetwork
from utils import get_logger
from utils import key_generator
from utils import load_json
from utils import mformat_validator
from utils import normalize_msg_path
from utils import ping_redis
from utils import RABBITMQ_URI
from utils import REDIS_HOST
from utils import REDIS_PORT

# install as described in https://bgpstream.caida.org/docs/install/pybgpstream

START_TIME_OFFSET = 3600  # seconds
log = get_logger()
redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
DEFAULT_MON_TIMEOUT_LAST_BGP_UPDATE = 60 * 60


def run_bgpstream(prefixes_file=None, projects=[], start=0, end=0):
    """
    Retrieve all records related to a list of prefixes
    https://bgpstream.caida.org/docs/api/pybgpstream/_pybgpstream.html

    :param prefixes_file: <str> input prefix json
    :param start: <int> start timestamp in UNIX epochs
    :param end: <int> end timestamp in UNIX epochs (if 0 --> "live mode")

    :return: -
    """

    prefixes = load_json(prefixes_file)
    assert prefixes is not None

    # create a new bgpstream instance and a reusable bgprecord instance
    stream = _pybgpstream.BGPStream()

    # consider collectors from given projects
    for project in projects:
        stream.add_filter("project", project)

    # filter prefixes
    for prefix in prefixes:
        stream.add_filter("prefix", prefix)

    # filter record type
    stream.add_filter("record-type", "updates")

    # filter based on timing (if end=0 --> live mode)
    stream.add_interval_filter(start, end)

    # set live mode
    stream.set_live_mode()

    # start the stream
    stream.start()

    # print('BGPStream started...')
    # print('Projects ' + str(projects))
    # print('Prefixes ' + str(prefixes))
    # print('Start ' + str(start))
    # print('End ' + str(end))

    with Connection(RABBITMQ_URI) as connection:
        exchange = Exchange(
            "bgp-update", channel=connection, type="direct", durable=False
        )
        exchange.declare()
        producer = Producer(connection)
        validator = mformat_validator()
        while True:
            # get next record
            try:
                rec = stream.get_next_record()
            except BaseException:
                continue
            if (rec.status != "valid") or (rec.type != "update"):
                continue

            # get next element
            try:
                elem = rec.get_next_elem()
            except BaseException:
                continue

            while elem:
                if elem.type in {"A", "W"}:
                    redis.set(
                        "bgpstreamlive_seen_bgp_update",
                        "1",
                        ex=int(
                            os.getenv(
                                "MON_TIMEOUT_LAST_BGP_UPDATE",
                                DEFAULT_MON_TIMEOUT_LAST_BGP_UPDATE,
                            )
                        ),
                    )
                    this_prefix = str(elem.fields["prefix"])
                    service = "bgpstream|{}|{}".format(
                        str(rec.project), str(rec.collector)
                    )
                    type_ = elem.type
                    if type_ == "A":
                        as_path = elem.fields["as-path"].split(" ")
                        communities = [
                            {
                                "asn": int(comm.split(":")[0]),
                                "value": int(comm.split(":")[1]),
                            }
                            for comm in elem.fields["communities"]
                        ]
                    else:
                        as_path = []
                        communities = []
                    timestamp = float(rec.time)
                    peer_asn = elem.peer_asn

                    for prefix in prefixes:
                        base_ip, mask_length = this_prefix.split("/")
                        our_prefix = IPNetwork(prefix)
                        if (
                            IPAddress(base_ip) in our_prefix
                            and int(mask_length) >= our_prefix.prefixlen
                        ):
                            msg = {
                                "type": type_,
                                "timestamp": timestamp,
                                "path": as_path,
                                "service": service,
                                "communities": communities,
                                "prefix": this_prefix,
                                "peer_asn": peer_asn,
                            }
                            if validator.validate(msg):
                                msgs = normalize_msg_path(msg)
                                for msg in msgs:
                                    key_generator(msg)
                                    log.debug(msg)
                                    producer.publish(
                                        msg,
                                        exchange=exchange,
                                        routing_key="update",
                                        serializer="json",
                                    )
                            else:
                                log.warning("Invalid format message: {}".format(msg))
                try:
                    elem = rec.get_next_elem()
                except BaseException:
                    continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BGPStream Live Monitor")
    parser.add_argument(
        "-p",
        "--prefixes",
        type=str,
        dest="prefixes_file",
        default=None,
        help="Prefix(es) to be monitored (json file with prefix list)",
    )
    parser.add_argument(
        "-m",
        "--mon_projects",
        type=str,
        dest="mon_projects",
        default=None,
        help="projects to consider for monitoring",
    )

    args = parser.parse_args()

    projects = args.mon_projects.split(",")
    ping_redis(redis)

    try:
        run_bgpstream(
            args.prefixes_file,
            projects,
            start=int(time.time()) - START_TIME_OFFSET,
            end=0,
        )
    except Exception:
        log.exception("exception")
    except KeyboardInterrupt:
        pass
