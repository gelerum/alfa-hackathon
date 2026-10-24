import os
from dotenv import load_dotenv
from master_parser import Master_Parser
from news_collector import RTParser, KommersantParser

load_dotenv()
mp = Master_Parser(os.environ['DATABASE_URL'], [RTParser(), KommersantParser()])
print('Добавлено:', mp.run())
print('Удалено старых:', mp.cleanup())