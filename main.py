from tqdm.auto import tqdm

import news_collector
import Tokinizer
import argparse
import exAI

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='Collects RT and Kommersant news from the given date until now '
                    'and sorts them by relevance to a company description. '
                    'News below the relevance threshold are dropped.',
        epilog='examples:\n'
               '  python main.py -d 2026-10-20 -c company.txt -p 0.55\n'
               '  python main.py -d 2026-10-20 -c company.txt -i short -m '
               f'{Tokinizer.LIGHT_MODEL}   (fast)\n\n'
               'relevance is the cosine similarity between a news item and the '
               'company description.\n'
               'the score range depends on the model and on --input, so pick the '
               'threshold by trial:\n'
               'run with -p 0 first, look at the scores, then raise it '
               '(0.55 is a reasonable start for the default model)',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-d', '--date', required=True, metavar='YYYY-MM-DD',
                        help='collect news starting from this date (Moscow time)')
    parser.add_argument('-c', '--descr', required=True, metavar='FILE',
                        help='UTF-8 .txt file with the company description')
    parser.add_argument('-p', '--perc', default=0, type=float, metavar='0..1',
                        help='minimum relevance to keep a news item (default: 0, keep all)')
    parser.add_argument('-m', '--model', default=Tokinizer.DEFAULT_MODEL, metavar='NAME',
                        help='sentence-transformers model from Hugging Face '
                             f'(default: {Tokinizer.DEFAULT_MODEL}, '
                             f'faster: {Tokinizer.LIGHT_MODEL})')
    parser.add_argument('-i', '--input', default='full', choices=['full', 'short'],
                        help='what to embed: full = news text, short = title + summary, '
                             '~4x faster (default: full)')
    parser.add_argument('-t', '--topics', default=5, type=int, metavar='N',
                        help='how many topics to show in the summary (default: 5)')
    args = parser.parse_args()

    Tokinizer.load_model(args.model)

    print(f'Собираю новости с {args.date}...')
    news = news_collector.get_news(args.date)
    print(f'Найдено новостей: {len(news)}')

    if args.input == 'short':
        texts = news['title'] + '. ' + news['summary'].fillna('')
    else:
        texts = news['text'].fillna(news['summary'])
    with open(args.descr,'r',encoding='utf-8') as f:
        company = f.read()
    company_emb = Tokinizer.embed(Tokinizer.tokenise(company))

    tokens = [Tokinizer.tokenise(t) for t in tqdm(texts, desc='Токенизация')]
    embeddings = Tokinizer.embed_many(tokens)
    news['embeding'] = list(embeddings)
    news['valid'] = embeddings @ company_emb

    news = news[news['valid'] >= args.perc]
    news.sort_values(by=['valid'],ascending=False,inplace=True)
    print(news[['date', 'source', 'title', 'valid', 'link']].to_string())
    print('\nГлавные темы:')
    topics = exAI.summarize(news, n_topics=args.topics)
    print(topics[['size', 'valid', 'title', 'link']].to_string())
