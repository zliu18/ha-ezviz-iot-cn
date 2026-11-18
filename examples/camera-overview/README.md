# 摄像头总览 · Camera overview

在一个 Lovelace 视图里查看多个镜头。点击画面选中镜头，再按播放键打开这一台。鼠标悬浮和选中时显示悬浮按钮，手机可点画面显示按钮。直播默认静音，开启声音后，在浏览器能读取音轨时显示音量条。停止直播会在当前页面保留最后一帧。

## 安装

1. 把 [ezviz-camera-overview.js](ezviz-camera-overview.js) 放到 HA 的 `/config/www/`。
2. 在 **设置 → 仪表板 → 资源** 添加 `/local/ezviz-camera-overview.js`，类型选 **JavaScript 模块**。
3. 新建一个可编辑的仪表板，在原始配置编辑器粘贴 [dashboard.yaml](dashboard.yaml)，把示例实体换成你自己的。也可以把其中的视图追加到现有仪表板。
4. 保存并重新加载页面。使用存储模式的仪表板时，管理员可以拖动或用上移／下移按钮保存镜头顺序。

| 字段 | 用途 |
| --- | --- |
| `entity` | 必填。支持 HA 直播的 `camera.*` 实体 |
| `name` | 镜头名称 |
| `image_entity` | 可选。萤石集成的定时抓拍图片实体。留空时从摄像头实体读取封面 |
| `night_vision_entity` | 可选。萤石集成的夜视选择实体，可在全彩与智能夜视间切换 |
| `defence_entity` | 可选。布防开关，图标打开该实体的详情 |

拍照会把 JPEG 下载到当前浏览器。实时画面、封面刷新和声音支持取决于摄像头集成及浏览器。全彩夜视打开时，关灯图标会保留在画面上。此示例可与 HA 的通用摄像头实体配合使用，萤石 IoT CN 提供可选的抓拍、夜视和布防实体。服务器端照片归档和手动主动防御需要另行接入。

## English quick start

Copy the JavaScript file to `/config/www/`, register `/local/ezviz-camera-overview.js` as a module, then paste the sample dashboard YAML into the raw configuration editor and replace the example entities. The card UI is in Simplified Chinese. Cameras play muted by default. Manual snapshots download as JPEGs, and the last rendered frame stays on the page when playback stops.
