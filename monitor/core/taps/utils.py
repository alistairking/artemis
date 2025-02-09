import copy
import hashlib
import json
import logging.config
import os
import time
from datetime import datetime
from datetime import timedelta
from ipaddress import ip_network as str2ip

import yaml

HISTORIC = os.getenv("HISTORIC", "false")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = os.getenv("RABBITMQ_PORT", 5672)
RABBITMQ_URI = "amqp://{}:{}@{}:{}//".format(
    RABBITMQ_USER, RABBITMQ_PASS, RABBITMQ_HOST, RABBITMQ_PORT
)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", 6379)


def get_logger(path="/etc/artemis/logging.yaml"):
    if os.path.exists(path):
        with open(path, "r") as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
        log = logging.getLogger("taps_logger")
        log.info("Loaded configuration from {}".format(path))
    else:
        FORMAT = "%(module)s - %(asctime)s - %(levelname)s @ %(funcName)s: %(message)s"
        logging.basicConfig(format=FORMAT, level=logging.INFO)
        log = logging
        log.info("Loaded default configuration")
    return log


log = get_logger()


def load_json(filename):
    json_obj = None
    try:
        with open(filename, "r") as f:
            json_obj = json.load(f)
    except Exception:
        return None
    return json_obj


def get_ip_version(prefix):
    if ":" in prefix:
        return "v6"
    return "v4"


def key_generator(msg):
    msg["key"] = get_hash(
        [
            msg["prefix"],
            msg["path"],
            msg["type"],
            "{0:.6f}".format(msg["timestamp"]),
            msg["peer_asn"],
        ]
    )


def get_hash(obj):
    return hashlib.shake_128(yaml.dump(obj).encode("utf-8")).hexdigest(16)


def ping_redis(redis_instance, timeout=5):
    while True:
        try:
            if not redis_instance.ping():
                raise BaseException("could not ping redis")
            break
        except Exception:
            log.error("retrying redis ping in {} seconds...".format(timeout))
            time.sleep(timeout)


def decompose_path(path):

    # first do an ultra-fast check if the path is a normal one
    # (simple sequence of ASNs)
    str_path = " ".join(map(str, path))
    if "{" not in str_path and "[" not in str_path and "(" not in str_path:
        return [path]

    # otherwise, check how to decompose
    decomposed_paths = []
    for hop in path:
        hop = str(hop)
        # AS-sets
        if "{" in hop:
            decomposed_hops = hop.lstrip("{").rstrip("}").split(",")
        # AS Confederation Set
        elif "[" in hop:
            decomposed_hops = hop.lstrip("[").rstrip("]").split(",")
        # AS Sequence Set
        elif "(" in hop or ")" in hop:
            decomposed_hops = hop.lstrip("(").rstrip(")").split(",")
        # simple ASN
        else:
            decomposed_hops = [hop]
        new_paths = []
        if not decomposed_paths:
            for dec_hop in decomposed_hops:
                new_paths.append([dec_hop])
        else:
            for prev_path in decomposed_paths:
                if "(" in hop or ")" in hop:
                    new_path = prev_path + decomposed_hops
                    new_paths.append(new_path)
                else:
                    for dec_hop in decomposed_hops:
                        new_path = prev_path + [dec_hop]
                        new_paths.append(new_path)
        decomposed_paths = new_paths
    return decomposed_paths


def normalize_msg_path(msg):
    msgs = []
    path = msg["path"]
    msg["orig_path"] = None
    if isinstance(path, list):
        dec_paths = decompose_path(path)
        if not dec_paths:
            msg["path"] = []
            msgs = [msg]
        elif len(dec_paths) == 1:
            msg["path"] = list(map(int, dec_paths[0]))
            msgs = [msg]
        else:
            for dec_path in dec_paths:
                copied_msg = copy.deepcopy(msg)
                copied_msg["path"] = list(map(int, dec_path))
                copied_msg["orig_path"] = path
                msgs.append(copied_msg)
    else:
        msgs = [msg]

    return msgs


class mformat_validator:

    mformat_fields = [
        "service",
        "type",
        "prefix",
        "path",
        "communities",
        "timestamp",
        "peer_asn",
    ]
    type_values = {"A", "W"}
    community_keys = {"asn", "value"}

    optional_fields_init = {"communities": []}

    def validate(self, msg):
        self.msg = msg
        if not self.valid_dict():
            return False

        self.add_optional_fields()

        for func in self.valid_generator():
            if not func():
                return False

        return True

    def valid_dict(self):
        if not isinstance(self.msg, dict):
            return False
        return True

    def add_optional_fields(self):
        for field in self.optional_fields_init:
            if field not in self.msg:
                self.msg[field] = self.optional_fields_init[field]

    def valid_fields(self):
        if any(field not in self.msg for field in self.mformat_fields):
            return False
        return True

    def valid_prefix(self):
        try:
            str2ip(self.msg["prefix"])
        except BaseException:
            return False
        return True

    def valid_service(self):
        if not isinstance(self.msg["service"], str):
            return False
        return True

    def valid_type(self):
        if self.msg["type"] not in self.type_values:
            return False
        return True

    def valid_path(self):
        if self.msg["type"] == "A" and not isinstance(self.msg["path"], list):
            return False
        return True

    def valid_communities(self):
        if not isinstance(self.msg["communities"], list):
            return False
        for comm in self.msg["communities"]:
            if not isinstance(comm, dict):
                return False
            if self.community_keys - set(comm.keys()):
                return False
        return True

    def valid_timestamp(self):
        if not isinstance(self.msg["timestamp"], float):
            return False
        if HISTORIC == "false" and datetime.utcfromtimestamp(
            self.msg["timestamp"]
        ) < datetime.utcnow() - timedelta(hours=1, minutes=30):
            return False
        return True

    def valid_peer_asn(self):
        if not isinstance(self.msg["peer_asn"], int):
            return False
        return True

    def valid_generator(self):
        yield self.valid_fields
        yield self.valid_prefix
        yield self.valid_service
        yield self.valid_type
        yield self.valid_path
        yield self.valid_communities
        yield self.valid_timestamp
        yield self.valid_peer_asn
