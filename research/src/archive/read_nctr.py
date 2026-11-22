import socket

DEVICE_IP = "192.168.200.1"
DEVICE_PORT = 20000

COMMANDS = [
    "AT+MINFO",
    "AT+SCON=?",
    "AT+MPROF=VG",
    "AT+MPROF=VD",
    "AT+MAIRCMD=?",
]

def query(command):
    payload = f"WizFi250AirCmd:{command}\r".encode("ascii")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3.0)

        print(f"\n=== {command} ===")
        sock.sendto(payload, (DEVICE_IP, DEVICE_PORT))

        try:
            while True:
                data, address = sock.recvfrom(8192)
                print(f"From {address}:")
                print(data.decode("utf-8", errors="replace"))
        except socket.timeout:
            pass

for command in COMMANDS:
    query(command)
