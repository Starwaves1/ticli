"""A very small terminal model, enough to tell a repaint from a reprint.

The display bug this exists for could not be caught by asserting on what
`_build_display` returned: the renderable was always right. What was wrong
was the *bytes* — a frame printed where the previous one should have been
overwritten, so every repaint pushed the last one into scrollback. So these
tests replay the escape sequences Rich actually wrote into a fixed-size
screen and ask what scrolled off the top.

Only the codes Rich's `Live` emits are implemented (CR, LF, cursor up/down,
erase line, SGR, cursor show/hide); anything else is skipped rather than
guessed at. Newline is CR+LF because the player leaves ONLCR on — cbreak
turns off ICANON and ECHO, not output processing.
"""

import re

CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class Screen:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.rows = [""] * height
        self.row = 0
        self.col = 0
        self.scrollback: list = []

    # ── plumbing ──

    def resize(self, width: int, height: int):
        """A terminal resize, with the part that matters: **reflow**.

        Narrowing is not a crop. iTerm2, Terminal.app, GNOME Terminal and
        the rest re-wrap what is on screen to the new width, so a line laid
        out for 100 columns becomes two rows at 60 and everything below it
        moves down — off the bottom, and the top of the screen into the
        scrollback. Any repaint that works by counting rows is wrong from
        that moment on, which is the whole reason this class exists. A model
        that merely clipped would let the bug pass.
        """
        lines, cursor = [], 0
        for i, row in enumerate(self.rows):
            if i == self.row:
                cursor = len(lines)
            row = row.rstrip()
            if not row:
                lines.append("")
                continue
            while len(row) > width:
                lines.append(row[:width])
                row = row[width:]
            lines.append(row)
        # The cursor keeps its place; whatever no longer fits above it goes
        top = max(0, min(cursor - height + 1, len(lines) - height))
        if top > 0:
            self.scrollback.extend(lines[:top])
        lines = lines[top:top + height]
        self.rows = lines + [""] * (height - len(lines))
        self.row = min(max(cursor - top, 0), height - 1)
        self.width = width
        self.height = height
        self.col = min(self.col, max(width - 1, 0))

    def _scroll(self):
        self.scrollback.append(self.rows[0])
        self.rows = self.rows[1:] + [""]
        self.row -= 1

    def _newline(self):
        self.row += 1
        self.col = 0
        if self.row >= self.height:
            self._scroll()

    def _put(self, ch: str):
        if self.col >= self.width:
            self._newline()
        line = self.rows[self.row]
        if len(line) < self.col:
            line += " " * (self.col - len(line))
        self.rows[self.row] = line[:self.col] + ch + line[self.col + 1:]
        self.col += 1

    # ── the parser ──

    def feed(self, data: str):
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                m = CSI.match(data, i)
                if m:
                    self._csi(m.group(1), m.group(2))
                    i = m.end()
                    continue
                i += 2  # a two-byte escape we don't model
                continue
            if ch == "\n":
                self._newline()
            elif ch == "\r":
                self.col = 0
            elif ch == "\t":
                self.col = min(self.col + 8 - self.col % 8, self.width)
            elif ch == "\b":
                self.col = max(self.col - 1, 0)
            elif ch >= " ":
                self._put(ch)
            i += 1

    def _csi(self, params: str, final: str):
        if params.startswith("?"):
            return  # cursor visibility and friends: no effect on the grid
        n = int(params.split(";")[0]) if params.split(";")[0].isdigit() else None
        if final == "A":
            self.row = max(self.row - (n or 1), 0)
        elif final == "B":
            self.row = min(self.row + (n or 1), self.height - 1)
        elif final == "C":
            self.col = min(self.col + (n or 1), self.width - 1)
        elif final == "D":
            self.col = max(self.col - (n or 1), 0)
        elif final == "G":
            self.col = max((n or 1) - 1, 0)
        elif final == "H":
            parts = params.split(";")
            self.row = max(int(parts[0] or 1) - 1, 0)
            self.col = max(int(parts[1] or 1) - 1, 0) if len(parts) > 1 else 0
        elif final == "K":
            if n in (None, 0):
                self.rows[self.row] = self.rows[self.row][:self.col]
            elif n == 1:
                self.rows[self.row] = " " * self.col + self.rows[self.row][self.col:]
            else:
                self.rows[self.row] = ""
        elif final == "J":
            if n == 2:
                self.rows = [""] * self.height
                self.row = self.col = 0

    # ── what tests ask ──

    def text(self) -> list:
        return [r.rstrip() for r in self.rows]

    def scrolled(self) -> list:
        return [r.rstrip() for r in self.scrollback]
