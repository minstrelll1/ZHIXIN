"""三个机载程序的地面日志：每个程序只保留最近一次启动，按接收时间标记。"""
import base64
import codecs
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid


class LatestProgramLog:
    VERSION = 1

    def __init__(self, path, source, redact):
        self.path = Path(path)
        self.cursor = self.path.with_suffix('.cursor.json')
        self.source, self.redact = source, redact
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.offset, self.session, self.line_open = 0, '', False
        self.generation = uuid.uuid4().hex
        try:
            saved = json.loads(self.cursor.read_text(encoding='utf-8'))
            if (saved.get('version') == self.VERSION and saved.get('source') == source
                    and self.path.stat().st_size == saved['local_size']):
                self.offset = int(saved['offset'])
                self.session, self.generation = saved['session'], saved['generation']
                self.line_open = saved.get('line_open', False)
                self.decoder.setstate((base64.b64decode(saved.get('pending_utf8', '')), 0))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # 旧版累积日志在收到新会话后替换，不沿用旧版同步偏移。

    def _save(self):
        saved = dict(version=self.VERSION, source=self.source, offset=self.offset,
                     session=self.session, generation=self.generation, line_open=self.line_open,
                     pending_utf8=base64.b64encode(self.decoder.getstate()[0]).decode('ascii'),
                     local_size=self.path.stat().st_size)
        temporary = self.cursor.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.cursor)

    def _write(self, text, label):
        if not text:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(sep=' ', timespec='milliseconds')
        pieces = text.split('\n')
        with self.path.open('a', encoding='utf-8', newline='') as stream:
            for i, part in enumerate(pieces):
                newline = i < len(pieces) - 1
                if not part and not newline:
                    continue
                if not self.line_open:
                    stream.write('[%s %s] ' % (label, stamp))
                stream.write(self.redact(part))
                if newline:
                    stream.write('\n')
                self.line_open = not newline

    def append_notice(self, text):
        # 管理提示另起一行，不与跨分页的程序打印粘连。
        if self.line_open:
            with self.path.open('a', encoding='utf-8', newline='') as stream:
                stream.write('\n')
            self.line_open = False
        self._write(text.rstrip('\n') + '\n', '地面记录')
        self._save()

    def consume(self, info):
        session = info.get('log_session')
        if not session:
            return  # SSH/状态查询失败不能清空已有日志。
        start = int(info.get('log_start', 0))
        read_start = int(info.get('log_read_start', start))
        if session != self.session:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b'')
            self.session, self.generation = session, uuid.uuid4().hex
            self.offset, self.line_open = start, False
            self.decoder.reset()
            self._write('仅保留本程序最近一次启动；时间为地面接收时间（北京时间），原始打印时间保留。\n', '地面记录')
        if read_start != self.offset:
            # 远端截断/换会话时，当前页可能从旧偏移开始；下次按新起点重新读取。
            self._save()
            return
        raw = base64.b64decode(info.get('log', ''))
        self._write(self.decoder.decode(raw), '地面接收')
        self.offset = int(info.get('offset', read_start + len(raw)))
        self._save()
