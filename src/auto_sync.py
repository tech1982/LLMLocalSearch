"""
Scheduled auto-sync for Telegram channels.
Runs ingestion at a configurable interval.
Usage:
  python src/auto_sync.py              # default: every 30 minutes
  python src/auto_sync.py --interval 60  # every 60 minutes
  python src/auto_sync.py --once         # run once and exit
"""
import subprocess
import sys
import time
import argparse
from datetime import datetime


def run_ingestion(script: str):
    """Run an ingestion script and return success status."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"[{ts}] Running {script}...")
    print(f"{'='*50}")
    result = subprocess.run(
        [sys.executable, f"src/{script}"],
        cwd="/app"
    )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=30, help="Sync interval in minutes")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--no-instagram", action="store_true", default=True, help="Skip Instagram (default)")
    parser.add_argument("--with-instagram", action="store_true", help="Include Instagram sync")
    args = parser.parse_args()

    scripts = ["ingest_telegram.py"]
    if args.with_instagram:
        scripts.append("ingest_instagram.py")

    print(f"🔄 Auto-sync started")
    print(f"   Scripts: {scripts}")
    if not args.once:
        print(f"   Interval: every {args.interval} minutes")
    print()

    while True:
        for script in scripts:
            try:
                run_ingestion(script)
            except Exception as e:
                print(f"❌ Error running {script}: {e}")

        if args.once:
            print("\n✅ Single run complete.")
            break

        next_run = datetime.now().strftime("%H:%M")
        print(f"\n⏳ Next sync in {args.interval} min (sleeping since {next_run})...")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
