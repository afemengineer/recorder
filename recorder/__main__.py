"""Start the local recorder. Bind to loopback; put an HTTPS proxy in front for phones."""
import argparse
import os
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Script-guided narration recorder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get("RECORDER_PASSWORD"):
        parser.error("RECORDER_PASSWORD is required when binding outside localhost")
    uvicorn.run("recorder.app:app", host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()