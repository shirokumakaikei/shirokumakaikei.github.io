# サイトの仕組み

このリポジトリは公開サイトだけを置く。記事を書く仕組み(AI社員・ネタ帳・体験メモ)は非公開リポジトリにある。

- 新しい記事: `content/articles/<slug>.md`。非公開側で承認されると自動でここに入る
- 既存の手書き記事: `articles/*.html`。一覧への掲載は `content/legacy.yml`
- `deploy`: main に入るたび、と毎朝6:10(JST)にビルドして GitHub Pages に公開(日付指定の予約記事もこれで出る)
- `check`: PRごとにビルド検証
- 決裁デスク: `desk/`(検索エンジンには載せない)

ローカルで確認:

```
pip install -r tools/requirements.txt
python tools/build.py --out _site
```
