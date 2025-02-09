import hashlib
import json
import logging.config
import logging.handlers
import os
import re
import time
from contextlib import contextmanager
from ipaddress import ip_network as str2ip
from logging.handlers import SMTPHandler
from xmlrpc.client import ServerProxy

import psycopg2
import requests
import yaml

BACKEND_SUPERVISOR_HOST = os.getenv("BACKEND_SUPERVISOR_HOST", "localhost")
BACKEND_SUPERVISOR_PORT = os.getenv("BACKEND_SUPERVISOR_PORT", 9001)
MON_SUPERVISOR_HOST = os.getenv("MON_SUPERVISOR_HOST", "monitor")
MON_SUPERVISOR_PORT = os.getenv("MON_SUPERVISOR_PORT", 9001)
HISTORIC = os.getenv("HISTORIC", "false")
DB_NAME = os.getenv("DB_NAME", "artemis_db")
DB_USER = os.getenv("DB_USER", "artemis_user")
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", 5432)
DB_PASS = os.getenv("DB_PASS", "Art3m1s")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = os.getenv("RABBITMQ_PORT", 5672)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", 6379)
DEFAULT_HIJACK_LOG_FIELDS = json.dumps(
    [
        "prefix",
        "hijack_as",
        "type",
        "time_started",
        "time_last",
        "peers_seen",
        "configured_prefix",
        "timestamp_of_config",
        "asns_inf",
        "time_detected",
        "key",
        "community_annotation",
        "end_tag",
        "outdated_parent",
        "hijack_url",
    ]
)
try:
    HIJACK_LOG_FIELDS = set(
        json.loads(os.getenv("HIJACK_LOG_FIELDS", DEFAULT_HIJACK_LOG_FIELDS))
    )
except Exception:
    HIJACK_LOG_FIELDS = set(DEFAULT_HIJACK_LOG_FIELDS)
ARTEMIS_WEB_HOST = os.getenv("ARTEMIS_WEB_HOST", "artemis.com")

RABBITMQ_URI = "amqp://{}:{}@{}:{}//".format(
    RABBITMQ_USER, RABBITMQ_PASS, RABBITMQ_HOST, RABBITMQ_PORT
)
BACKEND_SUPERVISOR_URI = "http://{}:{}/RPC2".format(
    BACKEND_SUPERVISOR_HOST, BACKEND_SUPERVISOR_PORT
)
MON_SUPERVISOR_URI = "http://{}:{}/RPC2".format(
    MON_SUPERVISOR_HOST, MON_SUPERVISOR_PORT
)
RIPE_ASSET_REGEX = r"^RIPE_WHOIS_AS_SET_(.*)$"
ASN_REGEX = r"^AS(\d+)$"


class TLSSMTPHandler(SMTPHandler):
    def emit(self, record):
        """
        Emit a record.
        Format the record and send it to the specified addressees.
        """
        try:
            import smtplib

            try:
                from email.utils import formatdate
            except ImportError:
                formatdate = self.date_time
            port = self.mailport
            if not port:
                port = smtplib.SMTP_PORT
            smtp = smtplib.SMTP(self.mailhost, port)
            msg = self.format(record)
            msg = "From: %s\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\n\r\n%s" % (
                self.fromaddr,
                ",".join(self.toaddrs),
                self.getSubject(record),
                formatdate(),
                msg,
            )
            if self.username:
                smtp.ehlo()  # for tls add this line
                smtp.starttls()  # for tls add this line
                smtp.ehlo()  # for tls add this line
                smtp.login(self.username, self.password)
            smtp.sendmail(self.fromaddr, self.toaddrs, msg)
            smtp.quit()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)


class SSLSMTPHandler(SMTPHandler):
    def emit(self, record):
        """
        Emit a record.
        Format the record and send it to the specified addressees.
        """
        try:
            import smtplib

            try:
                from email.utils import formatdate
            except ImportError:
                formatdate = self.date_time
            port = self.mailport
            if not port:
                port = smtplib.SMTP_PORT
            smtp = smtplib.SMTP(self.mailhost, port)
            msg = self.format(record)
            msg = "From: %s\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\n\r\n%s" % (
                self.fromaddr,
                ",".join(self.toaddrs),
                self.getSubject(record),
                formatdate(),
                msg,
            )
            if self.username:
                smtp.ehlo()  # for tls add this line
                smtp.login(self.username, self.password)
            smtp.sendmail(self.fromaddr, self.toaddrs, msg)
            smtp.quit()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)


def get_logger(path="/etc/artemis/logging.yaml"):
    if os.path.exists(path):
        with open(path, "r") as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
        log = logging.getLogger("artemis_logger")
        log.info("Loaded configuration from {}".format(path))
    else:
        FORMAT = "%(module)s - %(asctime)s - %(levelname)s @ %(funcName)s: %(message)s"
        logging.basicConfig(format=FORMAT, level=logging.INFO)
        log = logging
        log.info("Loaded default configuration")
    return log


log = get_logger()


class ModulesState:
    def __init__(self):
        self.backend_server = ServerProxy(BACKEND_SUPERVISOR_URI)
        self.mon_server = ServerProxy(MON_SUPERVISOR_URI)

    def call(self, module, action):
        try:
            if module == "all":
                if action == "start":
                    for ctx in {self.backend_server, self.mon_server}:
                        ctx.supervisor.startAllProcesses()
                elif action == "stop":
                    for ctx in {self.backend_server, self.mon_server}:
                        ctx.supervisor.stopAllProcesses()
            else:
                ctx = self.backend_server
                if module == "monitor":
                    ctx = self.mon_server

                if action == "start":
                    modules = self.is_any_up_or_running(module, up=False)
                    for mod in modules:
                        ctx.supervisor.startProcess(mod)

                elif action == "stop":
                    modules = self.is_any_up_or_running(module)
                    for mod in modules:
                        ctx.supervisor.stopProcess(mod)

        except Exception:
            log.exception("exception")

    def is_any_up_or_running(self, module, up=True):
        ctx = self.backend_server
        if module == "monitor":
            ctx = self.mon_server

        try:
            if up:
                return [
                    "{}:{}".format(x["group"], x["name"])
                    for x in ctx.supervisor.getAllProcessInfo()
                    if x["group"] == module and (x["state"] == 20 or x["state"] == 10)
                ]
            return [
                "{}:{}".format(x["group"], x["name"])
                for x in ctx.supervisor.getAllProcessInfo()
                if x["group"] == module and (x["state"] != 20 and x["state"] != 10)
            ]
        except Exception:
            log.exception("exception")
            return False


@contextmanager
def get_ro_cursor(conn):
    with conn.cursor() as curr:
        try:
            yield curr
        except Exception:
            raise


@contextmanager
def get_wo_cursor(conn):
    with conn.cursor() as curr:
        try:
            yield curr
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def get_db_conn():
    conn = None
    time_sleep_connection_retry = 5
    while not conn:
        try:
            conn = psycopg2.connect(
                dbname=DB_NAME,
                user=DB_USER,
                host=DB_HOST,
                port=DB_PORT,
                password=DB_PASS,
            )
        except Exception:
            log.exception("exception")
            time.sleep(time_sleep_connection_retry)
        finally:
            log.debug("PostgreSQL DB created/connected..")
    return conn


def flatten(items, seqtypes=(list, tuple)):
    res = []
    if not isinstance(items, seqtypes):
        return [items]
    for item in items:
        if isinstance(item, seqtypes):
            res += flatten(item)
        else:
            res.append(item)
    return res


class ArtemisError(Exception):
    def __init__(self, _type, _where):
        self.type = _type
        self.where = _where

        message = "type: {}, at: {}".format(_type, _where)

        # Call the base class constructor with the parameters it needs
        super().__init__(message)


def exception_handler(log):
    def function_wrapper(f):
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception:
                log.exception("exception")
                return True

        return wrapper

    return function_wrapper


def dump_json(json_obj, filename):
    with open(filename, "w") as f:
        json.dump(json_obj, f)


def redis_key(prefix, hijack_as, _type):
    assert isinstance(prefix, str)
    assert isinstance(hijack_as, int)
    assert isinstance(_type, str)
    return get_hash([prefix, hijack_as, _type])


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


def purge_redis_eph_pers_keys(redis_instance, ephemeral_key, persistent_key):
    redis_pipeline = redis_instance.pipeline()
    # purge also tokens since they are not relevant any more
    redis_pipeline.delete("{}token_active".format(ephemeral_key))
    redis_pipeline.delete("{}token".format(ephemeral_key))
    redis_pipeline.delete(ephemeral_key)
    redis_pipeline.srem("persistent-keys", persistent_key)
    redis_pipeline.delete("hij_orig_neighb_{}".format(ephemeral_key))
    if redis_instance.exists("hijack_{}_prefixes_peers".format(ephemeral_key)):
        for element in redis_instance.sscan_iter(
            "hijack_{}_prefixes_peers".format(ephemeral_key)
        ):
            subelems = element.decode().split("_")
            prefix_peer_hijack_set = "prefix_{}_peer_{}_hijacks".format(
                subelems[0], subelems[1]
            )
            redis_pipeline.srem(prefix_peer_hijack_set, ephemeral_key)
            if redis_instance.scard(prefix_peer_hijack_set) <= 1:
                redis_pipeline.delete(prefix_peer_hijack_set)
        redis_pipeline.delete("hijack_{}_prefixes_peers".format(ephemeral_key))
    redis_pipeline.execute()


def valid_prefix(input_prefix):
    try:
        str2ip(input_prefix)
    except Exception:
        return False
    return True


def calculate_more_specifics(prefix, min_length, max_length):
    for prefix_length in range(min_length, max_length + 1):
        for sub_prefix in prefix.subnets(new_prefix=prefix_length):
            yield str(sub_prefix)


class SetEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return list(o)
        return super(SetEncoder, self).default(o)


def translate_rfc2622(input_prefix, just_match=False):
    """
    :param input_prefix: (str) input IPv4/IPv6 prefix that
    should be translated according to RFC2622
    :param just_match: (bool) check only if the prefix
    has matched instead of translating
    :return: output_prefixes: (iterator of str) output IPv4/IPv6 prefixes,
    if not just_match, otherwise True or False
    """

    # ^- is the exclusive more specifics operator; it stands for the more
    #    specifics of the address prefix excluding the address prefix
    #    itself.  For example, 128.9.0.0/16^- contains all the more
    #    specifics of 128.9.0.0/16 excluding 128.9.0.0/16.
    reg_exclusive = re.match(r"^(\S*)\^-$", input_prefix)
    if reg_exclusive:
        matched_prefix = reg_exclusive.group(1)
        if valid_prefix(matched_prefix):
            matched_prefix_ip = str2ip(matched_prefix)
            min_length = matched_prefix_ip.prefixlen + 1
            max_length = matched_prefix_ip.max_prefixlen
            if just_match:
                return True
            return calculate_more_specifics(matched_prefix_ip, min_length, max_length)

    # ^+ is the inclusive more specifics operator; it stands for the more
    #    specifics of the address prefix including the address prefix
    #    itself.  For example, 5.0.0.0/8^+ contains all the more specifics
    #    of 5.0.0.0/8 including 5.0.0.0/8.
    reg_inclusive = re.match(r"^(\S*)\^\+$", input_prefix)
    if reg_inclusive:
        matched_prefix = reg_inclusive.group(1)
        if valid_prefix(matched_prefix):
            matched_prefix_ip = str2ip(matched_prefix)
            min_length = matched_prefix_ip.prefixlen
            max_length = matched_prefix_ip.max_prefixlen
            if just_match:
                return True
            return calculate_more_specifics(matched_prefix_ip, min_length, max_length)

    # ^n where n is an integer, stands for all the length n specifics of
    #    the address prefix.  For example, 30.0.0.0/8^16 contains all the
    #    more specifics of 30.0.0.0/8 which are of length 16 such as
    #    30.9.0.0/16.
    reg_n = re.match(r"^(\S*)\^(\d+)$", input_prefix)
    if reg_n:
        matched_prefix = reg_n.group(1)
        length = int(reg_n.group(2))
        if valid_prefix(matched_prefix):
            matched_prefix_ip = str2ip(matched_prefix)
            min_length = length
            max_length = length
            if min_length < matched_prefix_ip.prefixlen:
                raise ArtemisError("invalid-n-small", input_prefix)
            if max_length > matched_prefix_ip.max_prefixlen:
                raise ArtemisError("invalid-n-large", input_prefix)
            if just_match:
                return True
            return list(
                map(
                    str,
                    calculate_more_specifics(matched_prefix_ip, min_length, max_length),
                )
            )

    # ^n-m where n and m are integers, stands for all the length n to
    #      length m specifics of the address prefix.  For example,
    #      30.0.0.0/8^24-32 contains all the more specifics of 30.0.0.0/8
    #      which are of length 24 to 32 such as 30.9.9.96/28.
    reg_n_m = re.match(r"^(\S*)\^(\d+)-(\d+)$", input_prefix)
    if reg_n_m:
        matched_prefix = reg_n_m.group(1)
        min_length = int(reg_n_m.group(2))
        max_length = int(reg_n_m.group(3))
        if valid_prefix(matched_prefix):
            matched_prefix_ip = str2ip(matched_prefix)
            if min_length < matched_prefix_ip.prefixlen:
                raise ArtemisError("invalid-n-small", input_prefix)
            if max_length > matched_prefix_ip.max_prefixlen:
                raise ArtemisError("invalid-n-large", input_prefix)
            if just_match:
                return True
            return calculate_more_specifics(matched_prefix_ip, min_length, max_length)

    # nothing has matched
    if just_match:
        return False

    return [input_prefix]


def translate_asn_range(asn_range, just_match=False):
    """
    :param <str> asn_range: <start_asn>-<end_asn>
    :param <bool> just_match: check only if the prefix
    has matched instead of translating
    :return: the list of ASNs corresponding to that range
    """
    reg_range = re.match(r"(\d+)\s*-\s*(\d+)", str(asn_range))
    if reg_range:
        start_asn = int(reg_range.group(1))
        end_asn = int(reg_range.group(2))
        if start_asn > end_asn:
            raise ArtemisError("end-asn before start-asn", asn_range)
        if just_match:
            return True
        return list(range(start_asn, end_asn + 1))

    # nothing has matched
    if just_match:
        return False

    return [asn_range]


def translate_as_set(as_set_id, just_match=False):
    """
    :param as_set_id: the ID of the AS-SET as present in the RIPE database (with a prefix in front for disambiguation)
    :param <bool> just_match: check only if the as_set name has matched instead of translating
    :return: the list of ASes that are present in the set
    """
    as_set_match = re.match(RIPE_ASSET_REGEX, as_set_id)
    if as_set_match:
        if just_match:
            return True
        try:
            as_set = as_set_match.group(1)
            as_members = set()
            response = requests.get(
                "https://stat.ripe.net/data/historical-whois/data.json?resource=as-set:{}".format(
                    as_set
                ),
                timeout=10,
            )
            json_response = response.json()
            for obj in json_response["data"]["objects"]:
                if obj["type"] == "as-set" and obj["latest"]:
                    for attr in obj["attributes"]:
                        if attr["attribute"] == "members":
                            value = attr["value"]
                            asn_match = re.match(ASN_REGEX, value)
                            if asn_match:
                                asn = int(asn_match.group(1))
                                as_members.add(asn)
                            else:
                                return {
                                    "success": False,
                                    "payload": {},
                                    "error": "invalid-asn-{}-in-as-set-{}".format(
                                        value, as_set
                                    ),
                                }
                else:
                    continue
            if as_members:
                return {
                    "success": True,
                    "payload": {"as_members": sorted(list(as_members))},
                    "error": False,
                }
            return {
                "success": False,
                "payload": {},
                "error": "empty-as-set-{}".format(as_set),
            }
        except Exception:
            return {
                "success": False,
                "payload": {},
                "error": "error-as-set-resolution-{}".format(as_set),
            }
    return False


def update_aliased_list(yaml_conf, obj, updated_obj):
    def recurse(y, ref, new_obj):
        if isinstance(y, dict):
            for i, k in [(idx, key) for idx, key in enumerate(y.keys()) if key is ref]:
                y.insert(i, new_obj, y.pop(k))
            for k, v in y.non_merged_items():
                if v is ref:
                    y[k] = new_obj
                else:
                    recurse(v, ref, new_obj)
        elif isinstance(y, list):
            for idx, item in enumerate(y):
                if item is ref:
                    y[idx] = new_obj
                else:
                    recurse(item, ref, new_obj)

    recurse(yaml_conf, obj, updated_obj)


def ping_redis(redis_instance, timeout=5):
    while True:
        try:
            if not redis_instance.ping():
                raise BaseException("could not ping redis")
            break
        except Exception:
            log.error("retrying redis ping in {} seconds...".format(timeout))
            time.sleep(timeout)


def search_worst_prefix(prefix, pyt_tree):
    if prefix in pyt_tree:
        worst_prefix = pyt_tree.get_key(prefix)
        while pyt_tree.parent(worst_prefix):
            worst_prefix = pyt_tree.parent(worst_prefix)
        return worst_prefix
    return None


def get_ip_version(prefix):
    if ":" in prefix:
        return "v6"
    return "v4"


def hijack_log_field_formatter(hijack_dict):
    logged_hijack_dict = {}
    try:
        fields_to_log = set(hijack_dict.keys()).intersection(HIJACK_LOG_FIELDS)
        for field in fields_to_log:
            logged_hijack_dict[field] = hijack_dict[field]
        # instead of storing in redis, simply add the hijack url upon logging
        if "hijack_url" in HIJACK_LOG_FIELDS and "key" in hijack_dict:
            logged_hijack_dict["hijack_url"] = "https://{}/main/hijack?key={}".format(
                ARTEMIS_WEB_HOST, hijack_dict["key"]
            )
    except Exception:
        log.exception("exception")
        return hijack_dict
    return logged_hijack_dict
