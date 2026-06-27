import json
f = open('toxic_result.json', encoding='utf-8')
data = json.load(f)
for item in data['toxic_clauses']:
    print()
    print('===', item['article_title'], '===')
    for c in item['toxic_clauses']:
        print(' ', c['severity'], '-', c['clause_summary'])
        print('  원문:', c['source_quote'][:60])