"""A harmless job for a live check of LeaseResources: prints a line a second for N seconds (default 15)."""
import sys
import time

n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
for i in range(n):
    print("live tick", i, flush=True)
    time.sleep(1)
print("live job done")
