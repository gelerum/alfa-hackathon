"""Список источников для парсинга. Новый источник — новая строка здесь."""

from docs_parser import PageDocsParser
from moex_parser import MOEXParser
from rss_parser import RSSParser
from sbercib_parser import SberCIBParser
from telegram_parser import TelegramParser

# Иностранные издания. За пейволлом (fetch_body=False) в body попадает анонс из ленты.
FOREIGN = [
    # других живых лент у Global Times не нашлось, эта обновляется редко
    RSSParser('Global Times', ['https://www.globaltimes.cn/rss/outbrain.xml'],
              'en', fetch_body=True),
    RSSParser('Financial Times', ['https://www.ft.com/rss/home',
                                  'https://www.ft.com/world?format=rss'], 'en'),
    RSSParser('The Guardian', ['https://www.theguardian.com/uk/business/rss',
                               'https://www.theguardian.com/world/rss'], 'en',
              fetch_body=True, attachments=True),   # бывают ссылки на документы госорганов
    RSSParser('WSJ', ['https://feeds.content.dowjones.io/public/rss/RSSMarketsMain',
                      'https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness',
                      'https://feeds.content.dowjones.io/public/rss/RSSWorldNews'], 'en'),
    RSSParser('The Economist', ['https://www.economist.com/finance-and-economics/rss.xml',
                                'https://www.economist.com/business/rss.xml'], 'en'),
    RSSParser('Forbes', ['https://www.forbes.com/business/feed/'], 'en'),
    RSSParser('BBC', ['https://feeds.bbci.co.uk/news/business/rss.xml',
                      'https://feeds.bbci.co.uk/news/world/rss.xml'], 'en', fetch_body=True),
    RSSParser('NYT', ['https://rss.nytimes.com/services/xml/rss/nyt/Business.xml',
                      'https://rss.nytimes.com/services/xml/rss/nyt/World.xml'], 'en'),
]

# Отечественные издания
RUSSIAN = [
    RSSParser('RBC', ['https://rssexport.rbc.ru/rbcnews/news/30/full.rss']),  # полный текст в ленте
    RSSParser('TASS', ['https://tass.ru/rss/v2.xml']),  # страницы рисуются скриптом — только анонс
    RSSParser('Interfax', ['https://www.interfax.ru/rss.asp'], fetch_body=True),
    RSSParser('URA.RU', ['https://ura.news/rss'], fetch_body=True),
    RSSParser('RIA Novosti', ['https://ria.ru/export/rss2/archive/index.xml'], fetch_body=True),
    RSSParser('Smart-Lab', ['https://smart-lab.ru/rss/',          # посты: полный текст в ленте
                            'https://smart-lab.ru/news/rss/']),   # новости: анонс
    MOEXParser(),       # moex.com/ru/news через API ISS
    SberCIBParser(),    # sbercib.ru/publications — все рубрики, включая news и report
]

# Отдельное внимание. e-disclosure.ru своего RSS не отдаёт — он идёт через
# канал t.me/edisclosure (Интерфакс-ЦРКИ) ниже. econs.online и rjmf.econs.online
# закрыты проверкой на бота — Econs идёт через канал t.me/econsonline.
SPECIAL = [
    RSSParser('CBR', ['https://www.cbr.ru/rss/RssPress',
                      'https://www.cbr.ru/rss/RssNews',
                      'https://www.cbr.ru/rss/eventrss'],
              fetch_body=True, attachments=True),   # прогнозы, «Вестник», указания — в PDF
    # новые документы в каталоге изданий и в мониторинге предприятий
    PageDocsParser('CBR documents', ['https://www.cbr.ru/about_br/publ/',
                                     'https://www.cbr.ru/dkp/mp/'],
                   subpages_prefix='/about_br/publ/'),
]

TELEGRAM_CHANNELS = [
    'centralbank_russia', 'MoscowExchangeOfficial', 'moex_derivatives', 'bezposhady',
    'newssmartlab', 'bankrollo', 'bcs_express', 'if_stocks', 'SberInvestments', 'if_bonds',
    'tb_invest_official', 't_analytics_official', 'oninvest_media', 'am_first', 'finamalert',
    'investnique', 'lemonfortea', 'siyar24', 'xtxixty', 'bankoffo', 'interfaxonline',
    'rbc_news', 'tass_agency', 'headlines_for_traders', 'uranews', 'fm_invest', 'minstroyrf',
    'MID_Russia', 'alexeylinetsky', 'ShumilovPavel', 'World_Sanctions', 'omyinvestments',
    'Bondholders', 'mozgovikresearch', 'minpromtorg_ru', 'government_rus', 'minfin',
    'selfinvestor', 'minec_russia', 'ifax_go', 'SberInvestmentsPRO', 'martynovtim',
    'profitanet', 'CashflowTime', 'thefinansist', 'gov_korotko', 'edisclosure', 'finside',
    'mintsifry', 'InsiderMoex', 'arimembers', 'econsonline',
]
TELEGRAM = [TelegramParser(channel) for channel in TELEGRAM_CHANNELS]

ALL_PARSERS = FOREIGN + RUSSIAN + SPECIAL + TELEGRAM
