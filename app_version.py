# -*- coding: utf-8 -*-
"""应用版本单一事实来源。

发版打 v* 标签时，只需改这里的 APP_VERSION（与 tag 保持一致）。
desktop.get_desktop_info 与版本自检共用，避免版本号散落多处、漏改导致
状态栏显示旧版本（历史 bug：项目已 v1.4.3，桌面信息仍写死 v1.4.2）。
"""

APP_VERSION = '1.4.90'
VERSION_LABEL = 'v' + APP_VERSION

# GitHub 仓库：版本自检用 /releases/latest 比对
REPO = 'Yzm889/-Smart-video-highlights'
RELEASES_URL = 'https://github.com/%s/releases/latest' % REPO
