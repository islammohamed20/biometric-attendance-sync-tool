
import local_config as config
import requests
import datetime
import json
import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from pickledb import PickleDB
from zk import ZK, const

EMPLOYEE_NOT_FOUND_ERROR_MESSAGES = ["No Employee found for the given employee field value"]
EMPLOYEE_INACTIVE_ERROR_MESSAGES = ["Transactions cannot be created for an Inactive Employee"]
DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGES = [
    "This employee already has a log with the same timestamp",
    "هذا الموظف لديه بالفعل سجل بنفس الطابع الزمني",
    "same timestamp",
    "الطابع الزمني"
]
EXCEPTIONS_MAP = {
    1: EMPLOYEE_NOT_FOUND_ERROR_MESSAGES,
    2: EMPLOYEE_INACTIVE_ERROR_MESSAGES,
    3: DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGES
}
allowlisted_errors = [msg for msgs in EXCEPTIONS_MAP.values() for msg in msgs]
if hasattr(config,'allowed_exceptions'):
    allowlisted_errors = [msg for num in config.allowed_exceptions for msg in EXCEPTIONS_MAP.get(num, [])]

device_punch_values_IN = getattr(config, 'device_punch_values_IN', [0,4])
device_punch_values_OUT = getattr(config, 'device_punch_values_OUT', [1,5])
ERPNEXT_VERSION = getattr(config, 'ERPNEXT_VERSION', 14)
EMPLOYEE_ARCHIVE_DIRECTORY = os.path.join(config.LOGS_DIRECTORY, 'Emloyee')
_employee_synced_cache = {}
ERPNEXT_REQUEST_TIMEOUT = getattr(config, 'ERPNEXT_REQUEST_TIMEOUT', 20)

# possible area of further developemt
    # Real-time events - setup getting events pushed from the machine rather then polling.
        #- this is documented as 'Real-time events' in the ZKProtocol manual.

# Notes:
# Status Keys in status.json
#  - lift_off_timestamp
#  - mission_accomplished_timestamp
#  - <device_id>_pull_timestamp
#  - <device_id>_push_timestamp
#  - <shift_type>_sync_timestamp

def main():
    """Takes care of checking if it is time to pull data based on config,
    then calling the relevent functions to pull data and push to EPRNext.

    """
    try:
        last_lift_off_timestamp = _safe_convert_date(status.get('lift_off_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
        if (last_lift_off_timestamp and last_lift_off_timestamp < datetime.datetime.now() - datetime.timedelta(minutes=config.PULL_FREQUENCY)) or not last_lift_off_timestamp:
            status.set('lift_off_timestamp', str(datetime.datetime.now()))
            status.save()
            info_logger.info("Cleared for lift off!")
            processed_device_ids = set()
            for device in config.devices:
                device_attendance_logs = None
                if device['device_id'] in processed_device_ids:
                    info_logger.info("Skipping duplicate device config for device_id: " + device['device_id'])
                    continue
                processed_device_ids.add(device['device_id'])
                info_logger.info("Processing Device: "+ device['device_id'])
                dump_file = get_dump_file_name_and_directory(device['device_id'], device['ip'])
                if os.path.exists(dump_file):
                    info_logger.error('Device Attendance Dump Found in Log Directory. This can mean the program crashed unexpectedly. Retrying with dumped data.')
                    with open(dump_file, 'r') as f:
                        file_contents = f.read()
                        if file_contents:
                            device_attendance_logs = list(map(lambda x: _apply_function_to_key(x, 'timestamp', datetime.datetime.fromtimestamp), json.loads(file_contents)))
                try:
                    pull_process_and_push_data(device, device_attendance_logs)
                    status.set(f'{device["device_id"]}_push_timestamp', str(datetime.datetime.now()))
                    status.save()
                    if os.path.exists(dump_file):
                        os.remove(dump_file)
                    info_logger.info("Successfully processed Device: "+ device['device_id'])
                except Exception:
                    error_logger.exception('exception when calling pull_process_and_push_data function for device'+json.dumps(device, default=str))
            if hasattr(config,'shift_type_device_mapping'):
                update_shift_last_sync_timestamp(config.shift_type_device_mapping)
            status.set('mission_accomplished_timestamp', str(datetime.datetime.now()))
            status.save()
            info_logger.info("Mission Accomplished!")
    except Exception:
        error_logger.exception('exception has occurred in the main function...')


def pull_process_and_push_data(device, device_attendance_logs=None):
    """ Takes a single device config as param and pulls data from that device.

    params:
    device: a single device config object from the local_config file
    device_attendance_logs: fetching from device is skipped if this param is passed. used to restart failed fetches from previous runs.
    """
    attendance_success_log_file = '_'.join(["attendance_success_log", device['device_id']])
    attendance_failed_log_file = '_'.join(["attendance_failed_log", device['device_id']])
    attendance_success_logger = setup_logger(attendance_success_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    attendance_failed_logger = setup_logger(attendance_failed_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_failed_log_file])+'.log')
    if not device_attendance_logs:
        device_attendance_logs = get_all_attendance_from_device(device['ip'], device_id=device['device_id'], clear_from_device_on_fetch=device['clear_from_device_on_fetch'])
        if not device_attendance_logs:
            return
    _ensure_employee_archive_dirs(device['device_id'])
    # for finding the last successfull push and restart from that point (or) from a set 'config.IMPORT_START_DATE' (whichever is later)
    index_of_last = -1
    last_processed_user_id = status.get(f'{device["device_id"]}_last_processed_user_id')
    last_processed_timestamp = _read_device_cursor_timestamp(device['device_id'])
    last_line = None
    # Optional legacy behavior: read cursor from success log tail if explicitly enabled.
    if getattr(config, 'USE_SUCCESS_LOG_CURSOR', False):
        last_line = get_last_line_from_file('/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    import_start_date = _safe_convert_date(config.IMPORT_START_DATE, "%Y%m%d")
    if last_processed_timestamp or last_line or import_start_date:
        last_user_id = None
        last_timestamp = None

        # Prefer durable status cursor across stop/start cycles.
        if last_processed_timestamp:
            last_user_id = str(last_processed_user_id) if last_processed_user_id is not None else None
            last_timestamp = last_processed_timestamp
        elif last_line:
            parsed = _parse_success_log_cursor(last_line)
            if parsed:
                last_user_id, last_timestamp = parsed

        if import_start_date:
            if last_timestamp:
                if last_timestamp < import_start_date:
                    last_timestamp = import_start_date
                    last_user_id = None
            else:
                last_timestamp = import_start_date
        for i, x in enumerate(device_attendance_logs):
            if last_user_id and last_timestamp:
                if last_user_id == str(x['user_id']) and last_timestamp == x['timestamp']:
                    index_of_last = i
                    break
            elif last_timestamp:
                if x['timestamp'] >= last_timestamp:
                    index_of_last = i
                    break

    pending_logs = device_attendance_logs[index_of_last+1:]
    _init_device_progress(device['device_id'], len(pending_logs))

    for device_attendance_log in pending_logs:
        _append_employee_device_log(device['device_id'], device_attendance_log)

        # Skip immediately if this exact record already synced successfully before.
        if _is_record_already_synced(device['device_id'], device_attendance_log):
            attendance_success_logger.info("\t".join(["ALREADY-SYNCED-SKIPPED", str(device_attendance_log['uid']),
                str(device_attendance_log['user_id']), str(device_attendance_log['timestamp'].timestamp()),
                str(device_attendance_log['punch']), str(device_attendance_log['status']),
                json.dumps(device_attendance_log, default=str)]))
            _set_device_cursor(device['device_id'], device_attendance_log)
            _bump_device_progress(device['device_id'])
            continue

        punch_direction = device['punch_direction']
        if punch_direction == 'AUTO':
            if device_attendance_log['punch'] in device_punch_values_OUT:
                punch_direction = 'OUT'
            elif device_attendance_log['punch'] in device_punch_values_IN:
                punch_direction = 'IN'
            else:
                punch_direction = None
        erpnext_status_code, erpnext_message = send_to_erpnext(device_attendance_log['user_id'], device_attendance_log['timestamp'], device['device_id'], punch_direction, latitude=device['latitude'], longitude=device['longitude'])
        if erpnext_status_code == 200:
            attendance_success_logger.info("\t".join([erpnext_message, str(device_attendance_log['uid']),
                str(device_attendance_log['user_id']), str(device_attendance_log['timestamp'].timestamp()),
                str(device_attendance_log['punch']), str(device_attendance_log['status']),
                json.dumps(device_attendance_log, default=str)]))
            _mark_record_synced(device['device_id'], device_attendance_log, erpnext_message)
            _set_device_cursor(device['device_id'], device_attendance_log)
            _bump_device_progress(device['device_id'])
        else:
            if erpnext_status_code == 417:
                # Record duplicate as processed so cursor advances and replay storm stops.
                attendance_success_logger.info("\t".join(["DUPLICATE-SKIPPED", str(device_attendance_log['uid']),
                    str(device_attendance_log['user_id']), str(device_attendance_log['timestamp'].timestamp()),
                    str(device_attendance_log['punch']), str(device_attendance_log['status']),
                    json.dumps(device_attendance_log, default=str)]))
                _mark_record_synced(device['device_id'], device_attendance_log, "DUPLICATE-SKIPPED")
                _set_device_cursor(device['device_id'], device_attendance_log)
                _bump_device_progress(device['device_id'])
                continue
            attendance_failed_logger.error("\t".join([str(erpnext_status_code), str(device_attendance_log['uid']),
                str(device_attendance_log['user_id']), str(device_attendance_log['timestamp'].timestamp()),
                str(device_attendance_log['punch']), str(device_attendance_log['status']),
                json.dumps(device_attendance_log, default=str)]))
            _append_employee_erp_failed_log(device['device_id'], device_attendance_log, erpnext_status_code)
            if not(any(error in erpnext_message for error in allowlisted_errors)):
                raise Exception('API Call to ERPNext Failed.')
            _set_device_cursor(device['device_id'], device_attendance_log)
            _bump_device_progress(device['device_id'])


def get_all_attendance_from_device(ip, port=4370, timeout=30, device_id=None, clear_from_device_on_fetch=False):
    #  Sample Attendance Logs [{'punch': 255, 'user_id': '22', 'uid': 12349, 'status': 1, 'timestamp': datetime.datetime(2019, 2, 26, 20, 31, 29)},{'punch': 255, 'user_id': '7', 'uid': 7, 'status': 1, 'timestamp': datetime.datetime(2019, 2, 26, 20, 31, 36)}]
    zk = ZK(ip, port=port, timeout=timeout)
    conn = None
    attendances = []
    try:
        conn = zk.connect()
        x = conn.disable_device()
        # device is disabled when fetching data
        info_logger.info("\t".join((ip, "Device Disable Attempted. Result:", str(x))))
        attendances = conn.get_attendance()
        info_logger.info("\t".join((ip, "Attendances Fetched:", str(len(attendances)))))
        status.set(f'{device_id}_push_timestamp', None)
        status.set(f'{device_id}_pull_timestamp', str(datetime.datetime.now()))
        status.save()
        if len(attendances):
            # keeping a backup before clearing data incase the programs fails.
            # if everything goes well then this file is removed automatically at the end.
            dump_file_name = get_dump_file_name_and_directory(device_id, ip)
            with open(dump_file_name, 'w+') as f:
                f.write(json.dumps(list(map(lambda x: x.__dict__, attendances)), default=datetime.datetime.timestamp))
            if clear_from_device_on_fetch:
                x = conn.clear_attendance()
                info_logger.info("\t".join((ip, "Attendance Clear Attempted. Result:", str(x))))
        x = conn.enable_device()
        info_logger.info("\t".join((ip, "Device Enable Attempted. Result:", str(x))))
    except:
        error_logger.exception(str(ip)+' exception when fetching from device...')
        raise Exception('Device fetch failed.')
    finally:
        if conn:
            conn.disconnect()
    return list(map(lambda x: x.__dict__, attendances))


def send_to_erpnext(employee_field_value, timestamp, device_id=None, log_type=None, latitude=None, longitude=None):
    """
    Examples: 
    
    For ERPNext, Frappe HR <= v14
    send_to_erpnext('12349',datetime.datetime.now(),'HO1','IN')

    For ERPNext, Frappe HR v15 onwards
    If 'Allow Geolocation Tracking' is on
    send_to_erpnext('12349',datetime.datetime.now(),'HO1','IN',latitude=12.34, longitude=56.78)
    """
    endpoint_app = "hrms" if ERPNEXT_VERSION > 13 else "erpnext"
    url = f"{config.ERPNEXT_URL}/api/method/{endpoint_app}.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
    headers = {
        'Authorization': "token "+ config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }
    data = {
        'employee_field_value' : employee_field_value,
        'timestamp' : timestamp.__str__(),
        'device_id' : device_id,
        'log_type' : log_type,
        'latitude' : latitude,
        'longitude' : longitude
    }
    try:
        response = requests.request("POST", url, headers=headers, json=data, timeout=ERPNEXT_REQUEST_TIMEOUT)
    except requests.RequestException as e:
        error_logger.error('\t'.join([
            'ERPNext request exception',
            str(employee_field_value),
            str(timestamp.timestamp()),
            str(device_id),
            str(log_type),
            str(e)
        ]))
        return 0, str(e)
    if response.status_code == 200:
        return 200, json.loads(response._content)['message']['name']
    else:
        error_str = _safe_get_error_str(response)
        is_duplicate_error = any(m in error_str for m in DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGES)
        if any(m in error_str for m in EMPLOYEE_NOT_FOUND_ERROR_MESSAGES):
            error_logger.error('\t'.join(['Error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
            # TODO: send email?
        elif is_duplicate_error:
            # Duplicate check-ins are expected in replay/retry scenarios.
            info_logger.info('\t'.join(['Duplicate check-in skipped.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
        else:
            error_logger.error('\t'.join(['Error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
        return response.status_code, error_str

def update_shift_last_sync_timestamp(shift_type_device_mapping):
    """
    ### algo for updating the sync_current_timestamp
    - get a list of devices to check
    - check if all the devices have a non 'None' push_timestamp
        - check if the earliest of the pull timestamp is greater than sync_current_timestamp for each shift name
            - then update this min of pull timestamp to the shift

    """
    for shift_type_device_map in shift_type_device_mapping:
        all_devices_pushed = True
        pull_timestamp_array = []
        for device_id in shift_type_device_map['related_device_id']:
            if not status.get(f'{device_id}_push_timestamp'):
                all_devices_pushed = False
                break
            pull_timestamp_array.append(_safe_convert_date(status.get(f'{device_id}_pull_timestamp'), "%Y-%m-%d %H:%M:%S.%f"))
        if all_devices_pushed:
            min_pull_timestamp = min(pull_timestamp_array)
            if isinstance(shift_type_device_map['shift_type_name'], str): # for backward compatibility of config file
                shift_type_device_map['shift_type_name'] = [shift_type_device_map['shift_type_name']]
            for shift in shift_type_device_map['shift_type_name']:
                try:
                    sync_current_timestamp = _safe_convert_date(status.get(f'{shift}_sync_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
                    if (sync_current_timestamp and min_pull_timestamp > sync_current_timestamp) or (min_pull_timestamp and not sync_current_timestamp):
                        response_code = send_shift_sync_to_erpnext(shift, min_pull_timestamp)
                        if response_code == 200:
                            status.set(f'{shift}_sync_timestamp', str(min_pull_timestamp))
                            status.save()
                except:
                    error_logger.exception('Exception in update_shift_last_sync_timestamp, for shift:'+shift)

def send_shift_sync_to_erpnext(shift_type_name, sync_timestamp):
    url = config.ERPNEXT_URL + "/api/resource/Shift Type/" + shift_type_name
    headers = {
        'Authorization': "token "+ config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }
    data = {
        "last_sync_of_checkin" : str(sync_timestamp)
    }
    try:
        response = requests.request("PUT", url, headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            info_logger.info("\t".join(['Shift Type last_sync_of_checkin Updated', str(shift_type_name), str(sync_timestamp.timestamp())]))
        else:
            error_str = _safe_get_error_str(response)
            error_logger.error('\t'.join(['Error during ERPNext Shift Type API Call.', str(shift_type_name), str(sync_timestamp.timestamp()), error_str]))
        return response.status_code
    except:
        error_logger.exception("\t".join(['exception when updating last_sync_of_checkin in Shift Type', str(shift_type_name), str(sync_timestamp.timestamp())]))

def get_last_line_from_file(file):
    # concerns to address(may be much later):
        # how will last line lookup work with log rotation when a new file is created?
            #- will that new file be empty at any time? or will it have a partial line from the previous file?
    line = None
    if not os.path.exists(file) or os.stat(file).st_size == 0:
        return line
    if os.stat(file).st_size < 5000:
        # quick hack to handle files with one line
        with open(file, 'r') as f:
            for line in f:
                pass
    else:
        # optimized for large log files
        with open(file, 'rb') as f:
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
            line = f.readline().decode()
    return line


def _set_device_cursor(device_id, attendance_log):
    """Persist the last processed attendance record for reliable restart/resume."""
    status.set(f'{device_id}_last_processed_user_id', str(attendance_log.get('user_id')))
    ts = attendance_log.get('timestamp')
    if isinstance(ts, datetime.datetime):
        status.set(f'{device_id}_last_processed_timestamp_epoch', ts.timestamp())
        status.set(f'{device_id}_last_processed_timestamp', str(ts))
    else:
        status.set(f'{device_id}_last_processed_timestamp', str(ts))
    status.save()


def _init_device_progress(device_id, total_records):
    status.set(f'{device_id}_run_started_at', str(datetime.datetime.now()))
    status.set(f'{device_id}_run_total_records', int(total_records))
    status.set(f'{device_id}_run_processed_records', 0)
    status.save()


def _bump_device_progress(device_id):
    processed = status.get(f'{device_id}_run_processed_records') or 0
    status.set(f'{device_id}_run_processed_records', int(processed) + 1)
    # Update push timestamp during processing so UI reflects live progress.
    status.set(f'{device_id}_push_timestamp', str(datetime.datetime.now()))
    status.save()


def _read_device_cursor_timestamp(device_id):
    """Read cursor timestamp with backward compatibility for old formats."""
    epoch_value = status.get(f'{device_id}_last_processed_timestamp_epoch')
    if epoch_value is not None:
        try:
            return datetime.datetime.fromtimestamp(float(epoch_value))
        except Exception:
            pass

    raw_value = status.get(f'{device_id}_last_processed_timestamp')
    if not raw_value:
        return None

    # Try with microseconds, then without.
    parsed = _safe_convert_date(raw_value, "%Y-%m-%d %H:%M:%S.%f")
    if parsed:
        return parsed
    return _safe_convert_date(raw_value, "%Y-%m-%d %H:%M:%S")


def _parse_success_log_cursor(line):
    """Parse cursor from success log line safely."""
    try:
        parts = line.split("\t")
        if len(parts) < 6:
            return None
        user_id = parts[4]
        ts = datetime.datetime.fromtimestamp(float(parts[5]))
        return user_id, ts
    except Exception:
        return None


def _ensure_employee_archive_dirs(device_id):
    base = os.path.join(EMPLOYEE_ARCHIVE_DIRECTORY, device_id)
    os.makedirs(os.path.join(base, 'device_records'), exist_ok=True)
    os.makedirs(os.path.join(base, 'erp_synced'), exist_ok=True)
    os.makedirs(os.path.join(base, 'erp_failed'), exist_ok=True)


def _record_key(attendance_log):
    return "|".join([
        str(attendance_log.get('user_id')),
        str(attendance_log.get('timestamp').timestamp()),
        str(attendance_log.get('punch'))
    ])


def _employee_file(device_id, folder, user_id):
    return os.path.join(EMPLOYEE_ARCHIVE_DIRECTORY, device_id, folder, f"{user_id}.log")


def _append_employee_device_log(device_id, attendance_log):
    user_id = str(attendance_log.get('user_id'))
    log_path = _employee_file(device_id, 'device_records', user_id)
    with open(log_path, 'a+', encoding='utf-8') as fh:
        fh.write("\t".join([
            str(datetime.datetime.now()),
            str(attendance_log.get('uid')),
            str(attendance_log.get('timestamp').timestamp()),
            str(attendance_log.get('punch')),
            str(attendance_log.get('status'))
        ]) + "\n")


def _load_employee_synced_cache(device_id, user_id):
    cache_key = f"{device_id}::{user_id}"
    if cache_key in _employee_synced_cache:
        return _employee_synced_cache[cache_key]

    synced_keys = set()
    log_path = _employee_file(device_id, 'erp_synced', user_id)
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                parts = line.rstrip('\n').split('\t')
                if len(parts) >= 2:
                    synced_keys.add(parts[1])

    _employee_synced_cache[cache_key] = synced_keys
    return synced_keys


def _is_record_already_synced(device_id, attendance_log):
    user_id = str(attendance_log.get('user_id'))
    key = _record_key(attendance_log)
    synced_keys = _load_employee_synced_cache(device_id, user_id)
    return key in synced_keys


def _mark_record_synced(device_id, attendance_log, erp_result):
    user_id = str(attendance_log.get('user_id'))
    key = _record_key(attendance_log)
    synced_keys = _load_employee_synced_cache(device_id, user_id)
    if key in synced_keys:
        return

    synced_keys.add(key)
    log_path = _employee_file(device_id, 'erp_synced', user_id)
    with open(log_path, 'a+', encoding='utf-8') as fh:
        fh.write("\t".join([
            str(datetime.datetime.now()),
            key,
            str(attendance_log.get('uid')),
            str(attendance_log.get('timestamp').timestamp()),
            str(attendance_log.get('punch')),
            str(attendance_log.get('status')),
            str(erp_result)
        ]) + "\n")


def _append_employee_erp_failed_log(device_id, attendance_log, status_code):
    user_id = str(attendance_log.get('user_id'))
    log_path = _employee_file(device_id, 'erp_failed', user_id)
    with open(log_path, 'a+', encoding='utf-8') as fh:
        fh.write("\t".join([
            str(datetime.datetime.now()),
            _record_key(attendance_log),
            str(status_code),
            str(attendance_log.get('uid')),
            str(attendance_log.get('timestamp').timestamp()),
            str(attendance_log.get('punch')),
            str(attendance_log.get('status'))
        ]) + "\n")


def setup_logger(name, log_file, level=logging.INFO, formatter=None):

    if not formatter:
        formatter = logging.Formatter('%(asctime)s\t%(levelname)s\t%(message)s')

    handler = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=50)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

def get_dump_file_name_and_directory(device_id, device_ip):
    return config.LOGS_DIRECTORY + '/' + device_id + "_" + device_ip.replace('.', '_') + '_last_fetch_dump.json'

def _apply_function_to_key(obj, key, fn):
    obj[key] = fn(obj[key])
    return obj

def _safe_convert_date(datestring, pattern):
    try:
        return datetime.datetime.strptime(datestring, pattern)
    except:
        return None

def _safe_get_error_str(res):
    try:
        error_json = json.loads(res._content)
        if 'exc' in error_json: # this means traceback is available
            error_str = json.loads(error_json['exc'])[0]
        else:
            error_str = json.dumps(error_json)
    except:
        error_str = str(res.__dict__)
    return error_str

# setup logger and status
if not os.path.exists(config.LOGS_DIRECTORY):
    os.makedirs(config.LOGS_DIRECTORY)
error_logger = setup_logger('error_logger', '/'.join([config.LOGS_DIRECTORY, 'error.log']), logging.ERROR)
info_logger = setup_logger('info_logger', '/'.join([config.LOGS_DIRECTORY, 'logs.log']))
status = PickleDB('/'.join([config.LOGS_DIRECTORY, 'status.json']))

def infinite_loop(sleep_time=15):
    print("Service Running...")
    while True:
        try:
            main()
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("Stopping...")
            break
        except Exception as e:
            print(e)

if __name__ == "__main__":
    infinite_loop()
