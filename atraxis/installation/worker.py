"""Internal deadline for trusted worker functions processing hostile input bytes."""
import multiprocessing

from atraxis.installation.validation import InstallError, require

WORKER_TIMEOUT = 5.0
MAX_RESULT_BYTES = 64


def run(target, args, *, field, timeout_code, failure_code, timeout=WORKER_TIMEOUT):
    # Only engine-owned Python functions are targets; plans never select code.
    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=target, args=(send, *args), daemon=True)
    started = False
    try:
        process.start(); started = True
        send.close()
        require(receive.poll(timeout), timeout_code, field)
        # Fixed bounded text, never pickle results from the input-processing worker.
        outcome = receive.recv_bytes(MAX_RESULT_BYTES).decode('ascii')
        require(outcome == 'ok', outcome, field)
    except InstallError:
        raise
    except Exception:
        raise InstallError(failure_code, field) from None
    finally:
        receive.close(); send.close()
        if started:
            process.join(0.1)
            if process.is_alive():
                process.kill(); process.join(1)
            process.close()
