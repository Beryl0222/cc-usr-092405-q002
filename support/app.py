"""应用装配：存储、引擎、命令处理、视图的组合根。"""

from .engine import Engine
from .messages import CommandHandler
from .store import Store
from .views import Views

SERVICE_ID = "para-event-support"
SERVICE_NAME = "残特奥赛事保障"


class App:
    def __init__(self, db_path=":memory:", now_fn=None):
        self.store = Store(db_path)
        self.engine = Engine(self.store, now_fn=now_fn)
        self.commands = CommandHandler(self.engine)
        self.views = Views(self.store)

    def close(self):
        self.store.close()
