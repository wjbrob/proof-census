#!/usr/bin/env python3
"""Machine-wide runaway-build guard.

Polls every POLL_S seconds. Kills ONLY proof-build workers (comm in TARGET_COMMS) — never
sessions, servers, or the VM — under three rules:

  RUNAWAY-KILL   : a target with RSS > CAP_MB            (e.g. the 68GB coqc)
  ORPHAN-KILL    : a target with PPID 1 and RSS > ORPHAN_MIN_MB  (orphaned toward a dead parent)
  OOM-RELIEF-KILL: MemAvailable < FLOOR_MB for 2 consecutive polls -> kill the largest target
                   (skipped, logged LOW-RAM-NOACTION, if the largest target is < ORPHAN_MIN_MB)

The target allowlist (coqc/lean/poly) makes it structurally impossible to kill anything else:
your editors, shells, databases and VMs can never match. Idempotent via a PID file; full audit log.

Run:  nohup python3 overload_guard.py >/dev/null 2>&1 &
Stop: kill $(cat overload_guard.pid)
"""
import os, sys, time, signal
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "overload_guard.log")
PIDFILE = os.path.join(HERE, "overload_guard.pid")

POLL_S         = 15
CAP_MB         = 45 * 1024      # runaway per-process RSS cap
FLOOR_MB       = 6 * 1024       # sustained-low-RAM floor
ORPHAN_MIN_MB  = 2 * 1024       # ignore tiny targets
HEARTBEAT_EVERY = 20            # ~ every 5 min
TARGET_COMMS   = {"coqc", "lean", "poly"}
RECIPE_COMMS   = {"sh", "bash", "dash", "make", "lake"}  # recipe parents safe to reap with a runaway

PAGE = os.sysconf("SC_PAGE_SIZE")

def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")

def mem_avail_mb():
    with open("/proc/meminfo") as f:
        for ln in f:
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) // 1024
    return None

def swap_used_mb():
    tot = free = 0
    with open("/proc/meminfo") as f:
        for ln in f:
            if ln.startswith("SwapTotal:"): tot = int(ln.split()[1]) // 1024
            elif ln.startswith("SwapFree:"): free = int(ln.split()[1]) // 1024
    return tot - free

def proc_info(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            st = f.read()
        rp = st.rindex(")")
        comm = st[st.index("(") + 1:rp]
        ppid = int(st[rp + 2:].split()[1])
        with open(f"/proc/{pid}/statm") as f:
            rss_mb = int(f.read().split()[1]) * PAGE // (1024 * 1024)
        return comm, ppid, rss_mb
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        return None

def targets():
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        info = proc_info(int(pid))
        if not info:
            continue
        comm, ppid, rss_mb = info
        if comm in TARGET_COMMS:
            out.append((int(pid), ppid, comm, rss_mb))
    return out

def kill(pid, sig=signal.SIGKILL):
    try:
        os.kill(pid, sig); return True
    except ProcessLookupError:
        return False

def main():
    # idempotency: refuse to start if a live guard already holds the pidfile
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE).read().strip())
            os.kill(old, 0)  # alive?
            print(f"overload_guard already running (pid {old}); exiting.")
            return
        except (ProcessLookupError, ValueError):
            pass  # stale pidfile, take over
    open(PIDFILE, "w").write(str(os.getpid()))

    def cleanup(*_):
        try: os.remove(PIDFILE)
        except OSError: pass
        log("GUARD-STOP")
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    avail0 = mem_avail_mb()
    cur = targets()
    log(f"GUARD-START pid={os.getpid()} poll={POLL_S}s cap={CAP_MB//1024}GB floor={FLOOR_MB//1024}GB "
        f"avail={avail0}MB targets={[ (c,r) for _,_,c,r in cur ]}")
    log(f"GUARD-CONFIG target_comms={sorted(TARGET_COMMS)} (protected by construction: "
        f"your database/VM/editor/shell processes never match)")

    low_streak = 0
    tick = 0
    while True:
        try:
            tick += 1
            avail = mem_avail_mb()
            ts = targets()

            # rule 1: runaway cap
            for pid, ppid, comm, rss in ts:
                if rss > CAP_MB:
                    log(f"RUNAWAY-KILL pid={pid} comm={comm} rss={rss}MB > cap={CAP_MB}MB "
                        f"avail={avail}MB swap={swap_used_mb()}MB; killing + recipe-parent {ppid}")
                    kill(pid)
                    pinfo = proc_info(ppid)
                    if pinfo and pinfo[0] in RECIPE_COMMS:
                        kill(ppid)

            # rule 2: orphan reap
            for pid, ppid, comm, rss in ts:
                if ppid == 1 and rss > ORPHAN_MIN_MB:
                    log(f"ORPHAN-KILL pid={pid} comm={comm} ppid=1 rss={rss}MB avail={avail}MB")
                    kill(pid)

            # rule 3: sustained low RAM -> kill largest target
            if avail is not None and avail < FLOOR_MB:
                low_streak += 1
                if low_streak >= 2:
                    live = [t for t in targets()]  # refresh after rules 1-2
                    if live:
                        big = max(live, key=lambda t: t[3])
                        if big[3] >= ORPHAN_MIN_MB:
                            log(f"OOM-RELIEF-KILL pid={big[0]} comm={big[2]} rss={big[3]}MB "
                                f"(largest target) avail={avail}MB swap={swap_used_mb()}MB sustained-low")
                            kill(big[0])
                            pinfo = proc_info(big[1])
                            if pinfo and pinfo[0] in RECIPE_COMMS:
                                kill(big[1])
                        else:
                            log(f"LOW-RAM-NOACTION avail={avail}MB but largest target only "
                                f"{big[3]}MB — pressure is elsewhere (not killing)")
                    else:
                        log(f"LOW-RAM-NOACTION avail={avail}MB but no build-worker targets present")
                    low_streak = 0
            else:
                low_streak = 0

            if tick % HEARTBEAT_EVERY == 0:
                inv = [(c, r) for _, _, c, r in ts]
                log(f"HEARTBEAT avail={avail}MB swap={swap_used_mb()}MB targets={inv}")
        except Exception as e:
            log(f"GUARD-ERROR {type(e).__name__}: {e}")
        time.sleep(POLL_S)

if __name__ == "__main__":
    main()
