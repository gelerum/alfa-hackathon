import os
from dotenv import load_dotenv
from master_parser import Master_Parser
from sources import ALL_PARSERS

load_dotenv()
mp = Master_Parser(os.environ['DATABASE_URL'], ALL_PARSERS)
stats = mp.run()
print(f'Добавлено всего: {sum(stats.values())} из {len(stats)} источников')
print('Удалено старых:', mp.cleanup())
