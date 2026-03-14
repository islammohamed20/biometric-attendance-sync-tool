# ERPNext related configs
ERPNEXT_API_KEY = 'c9570f2a4bc3cb4'
ERPNEXT_API_SECRET = '6f8eb3afad4aa89'
ERPNEXT_URL = 'https://hd-souq.com'


# operational configs
PULL_FREQUENCY = 60 # in minutes
LOGS_DIRECTORY = 'logs' # logs of this script is stored in this directory
IMPORT_START_DATE = '20251101' # format: '20190501' or None
ERPNEXT_VERSION = 15

# Biometric device configs (all keys mandatory)
    #- device_id - must be unique, strictly alphanumerical chars only. no space allowed.
    #- ip - device IP Address
    #- punch_direction - 'IN'/'OUT'/'AUTO'/None
    #- clear_from_device_on_fetch: if set to true then attendance is deleted after fetch is successful.
    #(Caution: this feature can lead to data loss if used carelessly.)
devices = [{"device_id": "Master", "ip": "192.168.0.220", "punch_direction": "AUTO", "clear_from_device_on_fetch": False, "latitude": 0.0, "longitude": 0.0}, {"device_id": "Master", "ip": "192.168.0.220", "punch_direction": "AUTO", "clear_from_device_on_fetch": False, "latitude": 0.0, "longitude": 0.0}]

# Configs updating sync timestamp in the Shift Type DocType
shift_type_device_mapping = [{"shift_type_name": "Master11", "related_device_id": ["Master"]}, {"shift_type_name": "Master2", "related_device_id": ["Master"]}]
