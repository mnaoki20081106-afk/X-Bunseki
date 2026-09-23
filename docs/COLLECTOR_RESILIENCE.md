# X収集の互換性と復旧

Xの非公開Webインターフェースに依存するため、永久稼働・未知の変更への完全自動対応は保証できません。ログイン失効やアカウント確認は、本人による再ログインが必要です。認証や制限を回避する処理は追加していません。

## 自動対応する範囲

- 各巡回の開始時にXの公開Webページと公式CDNのmain bundleから、SearchTimeline / TweetDetailのqueryIdを読み取ります。JavaScriptは実行せず、Cookieも渡しません。全体8秒・サイズ上限つきです。取得不可なら固定SDKの既存IDで実行します。
- timelineのentries / entry / items / moduleItemsとtweet / resultラッパーに対応します。未知の構造は取得障害として扱います。引用元を別の観測として誤収集しません。
- HTTPとGraphQLの両方で認証失効・アカウント制限・アクセス拒否・レート制限・上流エラーを判別します。認証やアクセス拒否では以降の要求を停止します。レート制限は同じ操作への以降の要求をその巡回中停止し、次の定期巡回に持ち越します。並行実行中の要求は完了する場合があります。
- SDKと間接依存はpackage-lock.jsonで固定し、npm ciで再現します。SDKの更新はテストを通したうえで行います。

## データを守る動作

- 有効な応答の0件と、取得失敗の0件を区別します。
- 全件取得失敗ではmain.pyはhits・観測値・watchlist・学習スナップショットを更新せず、status.jsonに障害を記録します。既存の定期学習は過去の観測データを利用できます。
- 一部成功なら新しく確認できた投稿だけを処理し、statusはdegradedになります。
- 詳細取得に失敗したwatchlist投稿を、古い表示回数やゼロのいいね数で新規観測として出力しません。既存のwatchlistは次回追跡用に保持します。
- 投稿日時・表示回数・いいね・リポスト・返信・引用・ブックマークが欠落/不正な観測は学習に流しません。削除済み投稿は仕様変更と区別します。
- 検知閾値、予測モデル、学習昇格条件、検索クエリ、追跡間隔は変更しません。Xのランキング変更に対する検知精度の維持を保証するものではありません。

## 復旧設定

GitHub Actions Variables（Secretsではありません）:

| 設定 | 意味 |
| --- | --- |
| X_QUERY_ID_DISCOVERY | 通常は空欄。offで自動検出を停止し固定値を使う |
| X_GRAPHQL_QUERY_IDS | 検証済みのIDをJSONで指定。例: {"SearchTimeline":"検証済みID"}。自動検出より優先。対象はSearchTimelineとTweetDetailだけ |

認証失効: 既存の手順でログインし直し、GitHub Actions SecretのX_SESSION_STATEを更新します。Cookieをリポジトリに保存しないでください。

schema_changed: Xの応答形式/必須パラメータやSDKの変更を確認します。queryIdのみの変更で自動検出できない場合は上記Variableを利用できます。形式変更はx_response.mjsに対応を追加してテストします。無検証の最新SDKへの自動更新は行いません。

検証: npm ci --ignore-scripts、npm test、python -m unittest discover -s tests -p 'test_collector*.py'、python tests/test_pipeline.py。Nodeテストは実SDKのsearchPage/getTweetに対して通信を模擬し、Xにはアクセスしません。実アカウントの認証・Xの現行応答の受け入れは別途実行結果で確認が必要です。
