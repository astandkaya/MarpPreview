# Marp Preview

Sublime Text 用の Marp リアルタイムプレビュープラグイン。

- `.md` ファイルを保存するたびにブラウザのプレビューが自動更新
- カスタム CSS・画像・Mermaid 図をすべてオフラインでレンダリング
- **Node.js・npm・marp コマンド不要**

## 必要なもの

- [Sublime Text 4](https://www.sublimetext.com/)
- インターネット接続（初回起動時のみ。以降はオフライン動作）

## インストール

### Package Control 経由（推奨）

1. [Package Control](https://packagecontrol.io/installation) をインストール
2. コマンドパレット → **Package Control: Install Package**
3. **MarpPreview** を検索して Enter

### 手動インストール

リポジトリを clone して、Sublime Text の Packages ディレクトリにシンボリックリンクを作成します。

```bash
git clone https://github.com/astandkaya/MarpPreview.git ~/path/to/MarpPreview
```

**macOS**
```bash
ln -s ~/path/to/MarpPreview \
  ~/Library/Application\ Support/Sublime\ Text/Packages/MarpPreview
```

**Linux**
```bash
ln -s ~/path/to/MarpPreview \
  ~/.config/sublime-text/Packages/MarpPreview
```

**Windows**（PowerShell）
```powershell
New-Item -ItemType Junction -Path "$env:APPDATA\Sublime Text\Packages\MarpPreview" `
  -Target "C:\path\to\MarpPreview"
```

Sublime Text を再起動するとプラグインが読み込まれます。

## 初回起動時の自動ダウンロード

初回プレビュー時に以下を `~/.marp-binary/` へ自動ダウンロードします。

| ファイル | サイズ | 用途 |
|---------|--------|------|
| Marp CLI バイナリ | 約 60MB | Markdown → スライド変換・エクスポート |
| mermaid.min.js | 約 3.4MB | Mermaid 図のプレビュー表示 |

2回目以降はインターネット接続なしで動作します。

## 使い方

### プレビューを開く

`.md` ファイルを開いた状態で起動します。

- コマンドパレット → **Marp: Open Preview**

キーボードショートカットを追加するには `Preferences > Key Bindings` を開いて追加します：

```json
{ "keys": ["ctrl+shift+m"], "command": "marp_preview" }
```

### 自動更新

ファイルを保存するたびにブラウザのプレビューが自動更新されます。

### エクスポート

コマンドパレット → **Marp: Export...** でフォーマットを選択します。

| フォーマット | 出力 |
|-------------|------|
| HTML | 単一の自己完結 HTML ファイル |
| PowerPoint | PPTX ファイル |
| PDF | PDF ファイル |

出力先は `.md` ファイルと同じディレクトリです。

> **Mermaid 図を含む場合:** エクスポート時のみ [mermaid.ink](https://mermaid.ink) への接続が必要です。プレビューは引き続きオフライン動作します。

> **PPTX・PDF:** Marp CLI 内蔵の Chromium を使用するため、初回は時間がかかる場合があります。

### プレビューを停止

コマンドパレット → **Marp: Stop Preview**

## 対応機能

### カスタムテーマ CSS

```markdown
---
marp: true
---

<style>
@import url('./themes/my-theme.css');
</style>
```

`@import url()` で読み込んだ CSS ファイルと、その CSS 内の `url()` 画像も自動でインライン展開します。

### 背景画像

```markdown
![bg right w:400px](./images/photo.png)
```

ローカル画像を data URI に変換して配信します。

### Mermaid 図

````markdown
```mermaid
flowchart LR
    A[開始] --> B{判定}
    B -->|Yes| C[処理]
    B -->|No| D[終了]
```
````

| 場面 | レンダリング方法 |
|------|----------------|
| プレビュー | mermaid.min.js（ローカル）でブラウザ内レンダリング |
| エクスポート | mermaid.ink API（インターネット接続が必要） |

図はスライドに収まるよう最大 900×500px にスケーリングされます。

### Mermaid のサイズ調整

プレビューは `.mermaid-diagram svg`、エクスポートは `.mermaid-export` をターゲットにします。

```markdown
<style>
/* プレビューのサイズ調整 */
.mermaid-diagram svg { max-width: 600px; }

/* エクスポートのサイズ調整 */
.mermaid-export { width: 600px !important; }
</style>
```

## 設定

`Preferences > Package Settings > MarpPreview > Settings` から変更できます。

| 項目 | デフォルト | 説明 |
|------|-----------|------|
| `marp_command` | `null` | `null` で自動ダウンロードバイナリを使用。`"marp"` や `["npx", "@marp-team/marp-cli"]` で外部コマンドを指定することも可 |
| `server_port` | `8742` | プレビュー用 HTTP サーバーのポート番号 |

## バイナリの更新

`~/.marp-binary/` を削除すると次回起動時に再ダウンロードされます。

```bash
rm -rf ~/.marp-binary
```

## ライセンス

本プラグインは MIT ライセンスです。

実行時にダウンロードされる以下のソフトウェアも MIT ライセンスです。

- [Marp CLI](https://github.com/marp-team/marp-cli) © marp-team
- [Mermaid](https://github.com/mermaid-js/mermaid) © Knut Sveidqvist
