"""Owner-managed rules. Readers cannot create or change the authoritative store."""
from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

RULES_PATH = Path('/var/lib/ai-rules/rules.sqlite3')
RULE_LIMIT = 4000
TOTAL_LIMIT = 16000
COUNT_LIMIT = 50


class RuleConflict(ValueError):
    pass


class Rules:
    def __init__(self, path=RULES_PATH):
        self.path = Path(path)

    def initialize(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, content TEXT NOT NULL, revision TEXT NOT NULL)')
        # All projects may read rules; only the portal account owns this file.
        self.path.chmod(0o644)

    def connect(self, mode):
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=' + mode, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        with closing(self.connect('ro')) as db:
            return [dict(row) for row in db.execute('SELECT id, content, revision FROM rules ORDER BY rowid')]

    def change(self, action, *, content='', identity='', revision=''):
        if action not in ('add', 'save', 'delete'):
            raise ValueError('Неизвестное действие.')
        if action != 'delete':
            content = content.replace('\r\n', '\n').replace('\r', '\n')
            if not content.strip() or len(content) > RULE_LIMIT or any(
                    ord(character) < 32 and character not in '\n\t' for character in content):
                raise ValueError(f'Введите текст правила: от 1 до {RULE_LIMIT} символов, без управляющих знаков.')
        with closing(self.connect('rw')) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if action != 'add':
                row = db.execute('SELECT revision FROM rules WHERE id = ?', (identity,)).fetchone()
                if row is None or row['revision'] != revision:
                    raise RuleConflict('Правило уже изменено или удалено. Обновите страницу и повторите действие.')
            if action == 'delete':
                db.execute('DELETE FROM rules WHERE id = ?', (identity,))
                return
            count, length = db.execute(
                'SELECT count(*), coalesce(sum(length(content)), 0) FROM rules WHERE id != ?',
                (identity if action == 'save' else '',)).fetchone()
            if count >= COUNT_LIMIT or length + len(content) > TOTAL_LIMIT:
                raise ValueError(f'Можно сохранить до {COUNT_LIMIT} правил, всего до {TOTAL_LIMIT} символов.')
            version = uuid.uuid4().hex
            if action == 'add':
                db.execute('INSERT INTO rules VALUES (?, ?, ?)', (uuid.uuid4().hex, content, version))
            else:
                db.execute('UPDATE rules SET content = ?, revision = ? WHERE id = ?', (content, version, identity))

    def instructions(self):
        rules = self.list()
        if not rules:
            return ''
        return 'Общие правила владельца для всех проектов:\n\n' + '\n\n'.join(
            item['content'] for item in rules)


if __name__ == '__main__':
    Rules().initialize()
