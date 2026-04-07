#!/usr/bin/env python3
"""Dead simple web API for the solar monitor dashboard."""

import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from solar_monitor.database import (
    get_recent_readings,
    get_average_usage_by_hour,
    get_peak_usage,
    get_daily_summary,
    get_weather_history,
    init_db,
)

WEB_PORT = int(os.getenv("WEB_PORT", "8077"))
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WIDGET_DATA_PATH = os.path.join(_PROJECT_ROOT, "widget_data.json")


class SolarAPIHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.serve_dashboard()
        elif self.path == "/api/current":
            self.serve_json(self._get_current())
        elif self.path == "/api/readings":
            self.serve_json(get_recent_readings(24))
        elif self.path.startswith("/api/readings/"):
            hours = int(self.path.split("/")[-1])
            self.serve_json(get_recent_readings(hours))
        elif self.path == "/api/hourly":
            self.serve_json(get_average_usage_by_hour())
        elif self.path == "/api/peak":
            self.serve_json(get_peak_usage())
        elif self.path == "/api/summary":
            self.serve_json(get_daily_summary())
        elif self.path == "/api/weather":
            self.serve_json(get_weather_history(7))
        else:
            self.send_error(404)

    def _get_current(self):
        try:
            with open(WIDGET_DATA_PATH) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"error": "No data yet. Run monitor.py first."}

    def serve_json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def serve_dashboard(self):
        html_path = os.path.join(_PROJECT_ROOT, "web", "index.html")
        try:
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Dashboard not found. Create web/index.html")

    def log_message(self, format, *args):
        pass  # Quiet logging


def main():
    init_db()
    server = HTTPServer(("0.0.0.0", WEB_PORT), SolarAPIHandler)
    print(f"Solar Monitor dashboard: http://localhost:{WEB_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
