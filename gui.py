import datetime
import json
import os
import shlex
import sys
import subprocess
from collections import defaultdict
import local_config as config

from PyQt5 import QtCore
from PyQt5 import QtWidgets
from PyQt5.QtCore import QRegExp
from PyQt5.QtGui import QIntValidator, QRegExpValidator
from PyQt5.QtWidgets import QApplication, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton


APP_WINDOW = None


config_template = '''# ERPNext related configs
ERPNEXT_API_KEY = '{0}'
ERPNEXT_API_SECRET = '{1}'
ERPNEXT_URL = '{2}'


# operational configs
PULL_FREQUENCY = {3} # in minutes
LOGS_DIRECTORY = 'logs' # logs of this script is stored in this directory
IMPORT_START_DATE = {4} # format: '20190501' or None
ERPNEXT_VERSION = 15

# Biometric device configs (all keys mandatory)
    #- device_id - must be unique, strictly alphanumerical chars only. no space allowed.
    #- ip - device IP Address
    #- punch_direction - 'IN'/'OUT'/'AUTO'/None
    #- clear_from_device_on_fetch: if set to true then attendance is deleted after fetch is successful.
    #(Caution: this feature can lead to data loss if used carelessly.)
devices = {5}

# Configs updating sync timestamp in the Shift Type DocType
shift_type_device_mapping = {6}
'''


class BiometricWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.reg_exp_for_ip = r"((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(?=\s*netmask)"
        self.last_status_snapshot = ""
        self.employee_dialog = None
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.setInterval(5000)
        self.status_timer.timeout.connect(self.refresh_sync_status)
        self.init_ui()

    def closeEvent(self, event):
        can_exit = not hasattr(self, "p")
        if can_exit:
            event.accept()
        else:
            create_message_box(text="Window cannot be closed when \nservice is running!", title="Message", width=200)
            event.ignore()

    def init_ui(self):
        self.counter = 0
        self.setup_window()
        self.setup_textboxes_and_label()
        self.center()
        self.show()

    def setup_window(self):
        self.setFixedSize(470, 760)
        self.setWindowTitle('ERPNext Biometric Service')

    def setup_textboxes_and_label(self):

        self.create_label("API Secret", "api_secret", 20, 0, 200, 30)
        self.create_field("textbox_erpnext_api_secret", 20, 30, 200, 30)

        self.create_label("API Key", "api_key", 20, 60, 200, 30)
        self.create_field("textbox_erpnext_api_key", 20, 90, 200, 30)

        self.create_label("ERPNext URL", "erpnext_url", 20, 120, 200, 30)
        self.create_field("textbox_erpnext_url", 20, 150, 200, 30)

        self.create_label("Pull Frequency (in minutes)",
                          "pull_frequency", 250, 0, 200, 30)
        self.create_field("textbox_pull_frequency", 250, 30, 200, 30)

        self.create_label("Import Start Date",
                          "import_start_date", 250, 60, 200, 30)
        self.create_field("textbox_import_start_date", 250, 90, 200, 30)
        self.validate_data(r"^\d{1,2}/\d{1,2}/\d{4}$", "textbox_import_start_date")

        self.create_separator(210, 470)
        self.create_button('+', 'add', 390, 230, 35, 30, self.add_devices_fields)
        self.create_button('-', 'remove', 420, 230, 35, 30, self.remove_devices_fields)

        self.create_label("Device ID", "device_id", 20, 260, 0, 30)
        self.create_label("Device IP", "device_ip", 170, 260, 0, 30)
        self.create_label("Shift", "shift", 320, 260, 0, 0)

        # First Row for table
        self.create_field("device_id_0", 20, 290, 145, 30)
        self.create_field("device_ip_0", 165, 290, 145, 30)
        self.validate_data(self.reg_exp_for_ip, "device_ip_0")
        self.create_field("shift_0", 310, 290, 145, 30)

        # Sync status panel (shown below device table)
        self.create_label("Sync Status (Device / ERP)", "sync_status_label", 20, 475, 200, 20)
        self.sync_status_box = QtWidgets.QPlainTextEdit(self)
        self.sync_status_box.move(20, 500)
        self.sync_status_box.resize(430, 160)
        self.sync_status_box.setReadOnly(True)
        self.sync_status_box.setPlainText("No sync status yet.")
        self.sync_status_box.show()

        # Actions buttons
        self.create_button('Set Configuration', 'set_conf', 20, 690, 100, 30, self.setup_local_config)
        self.create_button('Running Status', 'running_status', 130, 690, 100, 30, self.get_running_status, enable=False)
        self.create_button('Employee', 'employee_status', 240, 690, 100, 30, self.open_employee_status)
        self.create_button('Start Service', 'start_or_stop_service', 350, 690, 100, 30, self.integrate_biometric, enable=False)
        self.set_default_value_or_placeholder_of_field()

        # validating integer
        self.onlyInt = QIntValidator(10, 30)
        self.textbox_pull_frequency.setValidator(self.onlyInt)

    def set_default_value_or_placeholder_of_field(self):
        if os.path.exists("local_config.py"):
            try:
                import local_config as config
                self.textbox_erpnext_api_secret.setText(getattr(config, 'ERPNEXT_API_SECRET', ''))
                self.textbox_erpnext_api_key.setText(getattr(config, 'ERPNEXT_API_KEY', ''))
                self.textbox_erpnext_url.setText(getattr(config, 'ERPNEXT_URL', ''))
                self.textbox_pull_frequency.setText(str(getattr(config, 'PULL_FREQUENCY', '')))
                d = getattr(config, 'IMPORT_START_DATE', None)
                if isinstance(d, str) and len(d) == 8:
                    ddmmyyyy = f"{d[6:8]}/{d[4:6]}/{d[0:4]}"
                    self.textbox_import_start_date.setText(ddmmyyyy)

                devices_conf = getattr(config, 'devices', [])
                shifts_conf = getattr(config, 'shift_type_device_mapping', [])

                if isinstance(devices_conf, list) and len(devices_conf) > 0:
                    first = devices_conf[0]
                    if isinstance(first, dict):
                        self.device_id_0.setText(first.get('device_id', ''))
                        self.device_ip_0.setText(first.get('ip', ''))
                        if isinstance(shifts_conf, list) and len(shifts_conf) > 0:
                            shift_name = shifts_conf[0].get('shift_type_name')
                            self.shift_0.setText(', '.join(shift_name) if isinstance(shift_name, list) else str(shift_name or ''))

                if isinstance(devices_conf, list) and len(devices_conf) > 1:
                    for _ in range(self.counter, len(devices_conf) - 1):
                        self.add_devices_fields()
                        device = getattr(self, 'device_id_' + str(self.counter))
                        ip = getattr(self, 'device_ip_' + str(self.counter))
                        shift = getattr(self, 'shift_' + str(self.counter))
                        row = devices_conf[self.counter] if self.counter < len(devices_conf) else None
                        if isinstance(row, dict):
                            device.setText(row.get('device_id', ''))
                            ip.setText(row.get('ip', ''))
                            if self.counter < len(shifts_conf):
                                shift_name = shifts_conf[self.counter].get('shift_type_name')
                                shift.setText(', '.join(shift_name) if isinstance(shift_name, list) else str(shift_name or ''))
            except Exception:
                pass
        else:
            self.textbox_erpnext_api_secret.setPlaceholderText("c70ee57c7b3124c")
            self.textbox_erpnext_api_key.setPlaceholderText("fb37y8fd4uh8ac")
            self.textbox_erpnext_url.setPlaceholderText("example.erpnext.com")
            self.textbox_pull_frequency.setPlaceholderText("60")
            try:
                dist_path = os.path.join(os.path.dirname(__file__), 'dist', 'local_config.py')
                if os.path.exists(dist_path):
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('dist_local_config', dist_path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    d = getattr(mod, 'IMPORT_START_DATE', None)
                    if isinstance(d, str) and len(d) == 8:
                        ddmmyyyy = f"{d[6:8]}/{d[4:6]}/{d[0:4]}"
                        self.textbox_import_start_date.setText(ddmmyyyy)
                    else:
                        first = datetime.datetime.now().replace(day=1)
                        self.textbox_import_start_date.setText(first.strftime("%d/%m/%Y"))
                else:
                    first = datetime.datetime.now().replace(day=1)
                    self.textbox_import_start_date.setText(first.strftime("%d/%m/%Y"))
            except Exception:
                first = datetime.datetime.now().replace(day=1)
                self.textbox_import_start_date.setText(first.strftime("%d/%m/%Y"))

        self.textbox_import_start_date.setPlaceholderText("DD/MM/YYYY")

    # Widgets Genrators
    def create_label(self, label_text, label_name, x, y, height, width):
        setattr(self,  label_name, QLabel(self))
        label = getattr(self, label_name)
        label.move(x, y)
        label.setText(label_text)
        if height and width:
            label.resize(height, width)
        label.show()

    def create_field(self, field_name, x, y, height, width):
        setattr(self,  field_name, QLineEdit(self))
        field = getattr(self, field_name)
        field.move(x, y)
        field.resize(height, width)
        field.show()

    def create_separator(self, y, width):
        setattr(self, 'separator', QLineEdit(self))
        field = getattr(self, 'separator')
        field.move(0, y)
        field.resize(width, 5)
        field.setEnabled(False)
        field.show()

    def create_button(self, button_label, button_name, x, y, height, width, callback_function, enable=True):
        setattr(self,  button_name, QPushButton(button_label, self))
        button = getattr(self, button_name)
        button.move(x, y)
        button.resize(height, width)
        button.clicked.connect(callback_function)
        button.setEnabled(enable)

    def center(self):
        frame = self.frameGeometry()
        screen = QApplication.desktop().screenNumber(QApplication.desktop().cursor().pos())
        centerPoint = QApplication.desktop().screenGeometry(screen).center()
        frame.moveCenter(centerPoint)
        self.move(frame.topLeft())

    def add_devices_fields(self):
        if self.counter < 5:
            self.counter += 1
            self.create_field("device_id_" + str(self.counter), 20, 290+(self.counter * 30), 145, 30)
            self.create_field("device_ip_" + str(self.counter), 165, 290+(self.counter * 30), 145, 30)
            self.validate_data(self.reg_exp_for_ip, "device_ip_" + str(self.counter))
            self.create_field("shift_" + str(self.counter), 310, 290+(self.counter * 30), 145, 30)
            self.refresh_sync_status()

    def validate_data(self, reg_exp, field_name):
        field = getattr(self, field_name)
        reg_ex = QRegExp(reg_exp)
        input_validator = QRegExpValidator(reg_ex, field)
        field.setValidator(input_validator)

    def remove_devices_fields(self):
        if self.counter > 0:
            b = getattr(self, "shift_" + str(self.counter))
            b.deleteLater()
            b = getattr(self, "device_id_" + str(self.counter))
            b.deleteLater()
            b = getattr(self, "device_ip_" + str(self.counter))
            b.deleteLater()

            self.counter -= 1
            self.refresh_sync_status()

    def integrate_biometric(self):
        button = getattr(self, "start_or_stop_service")

        if not hasattr(self, 'p'):
            print("Starting Service...")
            self._clear_startup_sync_files()
            # Start sync in child process and inherit CMD output.
            python_exe = sys.executable
            command = [python_exe, '-c', 'from erpnext_sync import infinite_loop; infinite_loop()']
            self.p = subprocess.Popen(command)
            print("Process running at {}".format(self.p.pid))
            button.setText("Stop Service")
            create_message_box("Service status", "Service has been started")
            self.create_label(str(datetime.datetime.now()), "service_start_time", 20, 60, 200, 30)
            self.service_start_time.setHidden(True)
            getattr(self, 'running_status').setEnabled(True)
            self.status_timer.start()
            self.refresh_sync_status()
        else:
            print("Stopping Service...")
            self.p.kill()
            del self.p
            button.setText("Start Service")
            create_message_box("Service status", "Service has been stoped")
            getattr(self, 'running_status').setEnabled(False)
            self.status_timer.stop()
            self.refresh_sync_status()

    def _clear_startup_sync_files(self):
        files_to_delete = [
            os.path.join(config.LOGS_DIRECTORY, 'attendance_success_log_Master.log'),
            os.path.join(config.LOGS_DIRECTORY, 'status.json')
        ]

        for file_path in files_to_delete:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"Deleted on start: {file_path}")
            except Exception as e:
                print(f"Failed deleting {file_path}: {e}")

    def setup_local_config(self):
        bio_config = self.get_local_config()

        print("Setting Local Configuration...")

        if not bio_config:
            print("Local Configuration not updated...")
            return 0

        if os.path.exists("local_config.py"):
            os.remove("local_config.py")

        with open("local_config.py", 'w+') as f:
            f.write(bio_config)

        print("Local Configuration Updated.")

        create_message_box("Message", "Configuration Updated!\nClick on Start Service.")

        getattr(self, 'start_or_stop_service').setEnabled(True)

    def get_device_details(self):
        device = {}
        devices = []
        shifts = []

        for idx in range(0, self.counter+1):
            shift = getattr(self, "shift_" + str(idx)).text()
            device_id = getattr(self, "device_id_" + str(idx)).text()
            devices.append({
                'device_id': device_id,
                'ip': getattr(self, "device_ip_" + str(idx)).text(),
                'punch_direction': 'AUTO',
                'clear_from_device_on_fetch': False,
                'latitude': 0.0,
                'longitude': 0.0
            })
            if shift in device:
                device[shift].append(device_id)
            else:
                device[shift]=[device_id]
        
        for shift_type_name in device.keys():
            shifts.append({
                'shift_type_name': shift_type_name,
                'related_device_id': device[shift_type_name]
            })
        return devices, shifts

    def get_local_config(self):
        if not validate_fields(self):
            return 0
        string = self.textbox_import_start_date.text()
        
        # Handle IMPORT_START_DATE - if empty or invalid, use None
        if not string or len(string.strip()) == 0:
            try:
                dist_path = os.path.join(os.path.dirname(__file__), 'dist', 'local_config.py')
                if os.path.exists(dist_path):
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('dist_local_config', dist_path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    d = getattr(mod, 'IMPORT_START_DATE', None)
                    if isinstance(d, str) and len(d) == 8:
                        import_start_date = "'{}'".format(d)
                    else:
                        first = datetime.datetime.now().replace(day=1)
                        import_start_date = "'{}'".format(first.strftime("%Y%m%d"))
                else:
                    first = datetime.datetime.now().replace(day=1)
                    import_start_date = "'{}'".format(first.strftime("%Y%m%d"))
            except Exception:
                first = datetime.datetime.now().replace(day=1)
                import_start_date = "'{}'".format(first.strftime("%Y%m%d"))
        else:
            # Only format date if it's valid (contains /)
            if '/' in string:
                formated_date = "".join([ele for ele in reversed(string.split("/"))])
                import_start_date = "'{}'".format(formated_date)
            else:
                import_start_date = "None"

        devices, shifts = self.get_device_details()
        # Convert devices to string while preserving Python boolean values (False instead of false)
        devices_str = str(devices).replace("'", '"')
        shifts_str = str(shifts).replace("'", '"')
        return config_template.format(self.textbox_erpnext_api_key.text(), self.textbox_erpnext_api_secret.text(), self.textbox_erpnext_url.text(), self.textbox_pull_frequency.text(), import_start_date, devices_str, shifts_str)

    def get_running_status(self):
        running_status = []
        with open('/'.join([config.LOGS_DIRECTORY])+'/logs.log', 'r') as f:
            index = 0
            for idx, line in enumerate(f,1):
                logdate = convert_into_date(line.split(',')[0], '%Y-%m-%d %H:%M:%S')
                if logdate and logdate >= convert_into_date(self.service_start_time.text().split('.')[0] , '%Y-%m-%d %H:%M:%S'):
                    index = idx
                    break
            if index:
                running_status.extend(read_file_contents('logs',index))

        with open('/'.join([config.LOGS_DIRECTORY])+'/error.log', 'r') as fread:
            error_index = 0
            for error_idx, error_line in enumerate(fread,1):
                start_date = convert_into_date(self.service_start_time.text().split('.')[0] , '%Y-%m-%d %H:%M:%S')
                if start_date and start_date.strftime('%Y-%m-%d') in error_line:
                    error_logdate = convert_into_date(error_line.split(',')[0], '%Y-%m-%d %H:%M:%S')
                    if error_logdate and error_logdate >= start_date:
                        error_index = error_idx
                        break
            if error_index:
                running_status.extend(read_file_contents('error',error_index))

        if running_status:
            create_message_box("Running status", ''.join(running_status))
        else:
            create_message_box("Running status", 'Process not yet started')

        self.refresh_sync_status(force_console=True)

    def _read_recent_lines(self, file_path, start_date=None):
        lines = []
        if not os.path.exists(file_path):
            return lines
        with open(file_path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not start_date:
                    lines.append(line.rstrip('\n'))
                    continue
                logdate = convert_into_date(line.split(',')[0], '%Y-%m-%d %H:%M:%S')
                if logdate and logdate >= start_date:
                    lines.append(line.rstrip('\n'))
        return lines

    def _get_selected_device_ids(self):
        device_ids = []
        seen = set()
        for idx in range(0, self.counter + 1):
            value = getattr(self, "device_id_" + str(idx)).text().strip()
            if value and value not in seen:
                device_ids.append(value)
                seen.add(value)
        return device_ids

    def _compose_sync_snapshot(self):
        start_date = None
        if hasattr(self, 'service_start_time'):
            start_date = convert_into_date(self.service_start_time.text().split('.')[0], '%Y-%m-%d %H:%M:%S')

        status_path = os.path.join(config.LOGS_DIRECTORY, 'status.json')
        logs_path = os.path.join(config.LOGS_DIRECTORY, 'logs.log')
        error_path = os.path.join(config.LOGS_DIRECTORY, 'error.log')

        status_data = {}
        if os.path.exists(status_path):
            try:
                with open(status_path, 'r', encoding='utf-8', errors='replace') as sf:
                    status_data = json.load(sf)
            except Exception:
                status_data = {}

        info_lines = self._read_recent_lines(logs_path, start_date=start_date)
        error_lines = self._read_recent_lines(error_path, start_date=start_date)

        header = []
        header.append('Service: RUNNING' if hasattr(self, 'p') else 'Service: STOPPED')
        if status_data.get('lift_off_timestamp'):
            header.append(f"Last lift-off: {status_data.get('lift_off_timestamp')}")
        if status_data.get('mission_accomplished_timestamp'):
            header.append(f"Last mission complete: {status_data.get('mission_accomplished_timestamp')}")
        if info_lines:
            header.append(f"Last info: {info_lines[-1]}")
        if error_lines:
            header.append(f"Last error: {error_lines[-1]}")

        device_sections = []
        for device_id in self._get_selected_device_ids():
            success_path = os.path.join(config.LOGS_DIRECTORY, f'attendance_success_log_{device_id}.log')
            failed_path = os.path.join(config.LOGS_DIRECTORY, f'attendance_failed_log_{device_id}.log')
            success_lines = self._read_recent_lines(success_path, start_date=start_date)
            failed_lines = self._read_recent_lines(failed_path, start_date=start_date)

            pull_ts = status_data.get(f'{device_id}_pull_timestamp', 'N/A')
            push_ts = status_data.get(f'{device_id}_push_timestamp', 'N/A')
            processed = status_data.get(f'{device_id}_run_processed_records', 0)
            total = status_data.get(f'{device_id}_run_total_records', 0)
            cursor_user = status_data.get(f'{device_id}_last_processed_user_id', 'N/A')
            cursor_ts = status_data.get(f'{device_id}_last_processed_timestamp', 'N/A')
            section = [
                f"[{device_id}] Device pull: {pull_ts}",
                f"[{device_id}] ERP push: {push_ts}",
                f"[{device_id}] Run progress: {processed}/{total}",
                f"[{device_id}] Last cursor: user={cursor_user}, ts={cursor_ts}",
                f"[{device_id}] Success count: {len(success_lines)}",
                f"[{device_id}] Failed count: {len(failed_lines)}"
            ]
            if success_lines:
                section.append(f"[{device_id}] Last success: {success_lines[-1]}")
            if failed_lines:
                section.append(f"[{device_id}] Last failed: {failed_lines[-1]}")
            device_sections.extend(section)

        if not device_sections:
            device_sections.append('No device configured yet.')

        return '\n'.join(header + ['-' * 55] + device_sections)

    def refresh_sync_status(self, force_console=False):
        snapshot = self._compose_sync_snapshot()
        self.sync_status_box.setPlainText(snapshot)

        # Print only on changes to avoid flooding CMD.
        if force_console or snapshot != self.last_status_snapshot:
            print("\n=== Sync Status Snapshot ===")
            print(snapshot)
            print("=== End Snapshot ===\n")
            self.last_status_snapshot = snapshot

    def _get_configured_devices(self):
        configured = []
        for idx in range(0, self.counter + 1):
            device_id = getattr(self, "device_id_" + str(idx)).text().strip()
            device_ip = getattr(self, "device_ip_" + str(idx)).text().strip()
            if device_id and device_ip:
                configured.append({"device_id": device_id, "ip": device_ip})
        return configured

    def _try_fetch_device_users(self, ip):
        users = []
        try:
            from zk import ZK
            zk = ZK(ip, port=4370, timeout=10)
            conn = None
            try:
                conn = zk.connect()
                device_users = conn.get_users() or []
                for user in device_users:
                    users.append({
                        "user_id": str(getattr(user, 'user_id', '') or ''),
                        "name": str(getattr(user, 'name', '') or '')
                    })
            finally:
                if conn:
                    conn.disconnect()
        except Exception:
            # Device may be offline/unreachable; logs will still be shown.
            return []
        return users

    def _parse_epoch(self, value):
        try:
            return datetime.datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return ""

    def _employee_sync_rows(self):
        rows = []
        devices = self._get_configured_devices()

        for device in devices:
            device_id = device['device_id']
            ip = device['ip']
            user_name_map = {}
            from_device_user_ids = set()

            for user in self._try_fetch_device_users(ip):
                uid = user.get('user_id', '').strip()
                if uid:
                    from_device_user_ids.add(uid)
                    user_name_map[uid] = user.get('name', '')

            stats = defaultdict(lambda: {
                'synced': 0,
                'duplicate_skipped': 0,
                'failed': 0,
                'last_success': '',
                'last_failed': ''
            })

            success_path = os.path.join(config.LOGS_DIRECTORY, f'attendance_success_log_{device_id}.log')
            failed_path = os.path.join(config.LOGS_DIRECTORY, f'attendance_failed_log_{device_id}.log')

            if os.path.exists(success_path):
                with open(success_path, 'r', encoding='utf-8', errors='replace') as sf:
                    for line in sf:
                        parts = line.rstrip('\n').split('\t')
                        if len(parts) < 6:
                            continue
                        msg = parts[2]
                        employee_id = parts[4].strip()
                        last_ts = self._parse_epoch(parts[5])
                        if not employee_id:
                            continue
                        if msg == 'DUPLICATE-SKIPPED':
                            stats[employee_id]['duplicate_skipped'] += 1
                        else:
                            stats[employee_id]['synced'] += 1
                        stats[employee_id]['last_success'] = last_ts

            if os.path.exists(failed_path):
                with open(failed_path, 'r', encoding='utf-8', errors='replace') as ff:
                    for line in ff:
                        parts = line.rstrip('\n').split('\t')
                        if len(parts) < 6:
                            continue
                        employee_id = parts[4].strip()
                        last_ts = self._parse_epoch(parts[5])
                        if not employee_id:
                            continue
                        stats[employee_id]['failed'] += 1
                        stats[employee_id]['last_failed'] = last_ts

            all_employee_ids = sorted(set(list(stats.keys()) + list(from_device_user_ids)), key=lambda x: int(x) if x.isdigit() else x)

            for emp_id in all_employee_ids:
                entry = stats[emp_id]
                rows.append({
                    'device_id': device_id,
                    'employee_id': emp_id,
                    'employee_name': user_name_map.get(emp_id, ''),
                    'synced': entry['synced'],
                    'duplicate_skipped': entry['duplicate_skipped'],
                    'failed': entry['failed'],
                    'last_success': entry['last_success'],
                    'last_failed': entry['last_failed']
                })

        return rows

    def open_employee_status(self):
        if self.employee_dialog and self.employee_dialog.isVisible():
            self.employee_dialog.raise_()
            self.employee_dialog.activateWindow()
            return

        self.employee_dialog = EmployeeStatusDialog(self._employee_sync_rows, self)
        self.employee_dialog.finished.connect(self._on_employee_dialog_closed)
        self.employee_dialog.show()

    def _on_employee_dialog_closed(self):
        self.employee_dialog = None


class EmployeeStatusDialog(QtWidgets.QDialog):
    def __init__(self, rows_provider, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Employees Sync Status')
        self.resize(980, 520)
        self._rows_provider = rows_provider
        self._rows = []
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self._build_ui()
        self.refresh_timer.start()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        self.summary_label = QtWidgets.QLabel(self)
        layout.addWidget(self.summary_label)

        self.last_update_label = QtWidgets.QLabel(self)
        layout.addWidget(self.last_update_label)

        self.table = QtWidgets.QTableWidget(self)
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            'Device',
            'Employee ID',
            'Employee Name',
            'Synced To ERP',
            'Duplicate Skipped',
            'Failed',
            'Last Success',
            'Last Failed'
        ])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons_layout = QtWidgets.QHBoxLayout()
        self.refresh_btn = QPushButton('Refresh', self)
        self.refresh_btn.clicked.connect(self.refresh_data)
        buttons_layout.addWidget(self.refresh_btn)

        close_btn = QPushButton('Close', self)
        close_btn.clicked.connect(self.accept)
        buttons_layout.addWidget(close_btn)

        layout.addLayout(buttons_layout)

        self.refresh_data()

    def closeEvent(self, event):
        if self.refresh_timer.isActive():
            self.refresh_timer.stop()
        super().closeEvent(event)

    def refresh_data(self):
        try:
            self._rows = self._rows_provider() if callable(self._rows_provider) else []
            self._populate_table()
            self.last_update_label.setText(
                f"Last update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            self.last_update_label.setText(f"Last update failed: {e}")

    def _populate_table(self):
        self.table.setRowCount(len(self._rows))

        total_synced = 0
        total_dups = 0
        total_failed = 0

        for row_idx, row in enumerate(self._rows):
            total_synced += int(row['synced'])
            total_dups += int(row['duplicate_skipped'])
            total_failed += int(row['failed'])

            values = [
                row['device_id'],
                row['employee_id'],
                row['employee_name'],
                str(row['synced']),
                str(row['duplicate_skipped']),
                str(row['failed']),
                row['last_success'],
                row['last_failed']
            ]

            for col_idx, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col_idx in (3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)

        self.summary_label.setText(
            f"Employees: {len(self._rows)} | Synced: {total_synced} | Duplicate Skipped: {total_dups} | Failed: {total_failed}"
        )

def read_file_contents(file_name, index):
    running_status = []
    with open('/'.join([config.LOGS_DIRECTORY])+f'/{file_name}.log', 'r') as file_handler:
        for idx, line in enumerate(file_handler,1):
            if idx>=index:
                running_status.append(line)
    return running_status


def validate_fields(self):
    def message(text):
        create_message_box("Missing Value", "Please Set {}".format(text), "warning")

    if not self.textbox_erpnext_api_key.text():
        return message("API Key")

    if not self.textbox_erpnext_api_secret.text():
        return message("API Secret")

    if not self.textbox_erpnext_url.text():
        return message("ERPNext URL")

    if self.textbox_import_start_date.text():
        return validate_date(self.textbox_import_start_date.text())
    return True


def validate_date(date):
    try:
        datetime.datetime.strptime(date, '%d/%m/%Y')
        return True
    except ValueError:
        create_message_box("", "Please Enter Date in correct format", "warning", width=200)
        return False


def convert_into_date(datestring, pattern):
    try:
        return datetime.datetime.strptime(datestring, pattern)
    except:
        return None


def create_message_box(title, text, icon="information", width=150):
    msg = QMessageBox()
    msg.setWindowTitle(title)
    lineCnt = len(text.split('\n'))
    if lineCnt > 15:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(1)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        tmpLabel = QtWidgets.QLabel(text)
        tmpLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(tmpLabel)
        msg.layout().addWidget(scroll, 12, 10, 1, msg.layout().columnCount())
        msg.setStyleSheet("QScrollArea{min-width:550 px; min-height: 400px}")
    else:
        msg.setText(text)
        if icon == "warning":
            msg.setIcon(QtWidgets.QMessageBox.Warning)
            msg.setStyleSheet("QMessageBox Warning{min-width: 50 px;}")
        else:
            msg.setIcon(QtWidgets.QMessageBox.Information)
            msg.setStyleSheet("QMessageBox Information{min-width: 50 px;}")
        msg.setStyleSheet("QmessageBox QLabel{min-width: "+str(width)+"px;}")
    msg.exec_()


def setup_window():
    biometric_app = QApplication(sys.argv)
    global APP_WINDOW
    APP_WINDOW = BiometricWindow()
    biometric_app.exec_()


def handle_fatal_exception(exc_type, exc_value, exc_traceback):
    # Let Ctrl+C and normal keyboard interrupt behave normally.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    try:
        print("\n[FATAL] GUI crashed. Shutting down service and closing CMD window...")
        if APP_WINDOW and hasattr(APP_WINDOW, 'p'):
            APP_WINDOW.p.kill()
            del APP_WINDOW.p
    except Exception:
        pass

    # Print full traceback to CMD for diagnostics.
    sys.__excepthook__(exc_type, exc_value, exc_traceback)
    # Non-zero exit ensures cmd /c window closes after crash.
    os._exit(1)


if __name__ == "__main__":
    sys.excepthook = handle_fatal_exception
    setup_window()
