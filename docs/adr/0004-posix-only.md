# macOS and Linux only; Windows is deliberately unsupported

The input path uses `termios`/`tty` and selects on stdin, so ticli cannot start on Windows. The owner chose to accept that rather than take on a port. Don't add new POSIX-only assumptions casually, but don't attempt a Windows port as a side quest either.
