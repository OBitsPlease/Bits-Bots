import threading
import time
import urllib.request
import webbrowser

from bitsbots.main import run


def open_dashboard_when_ready() -> None:
    url = "http://127.0.0.1:8765"
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{url}/api/auth/setup", timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    threading.Thread(target=open_dashboard_when_ready, daemon=True).start()
    run()
