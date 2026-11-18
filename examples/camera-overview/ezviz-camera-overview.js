// Optional Home Assistant camera overview. See README.md for installation.
const NIGHT_VISION_LABELS = {
  smart: "智能夜视",
  full_color: "全彩夜视",
  monochrome: "黑白夜视"
}

class EzvizCameraOverview extends HTMLElement {
  constructor() {
    super()
    this.attachShadow({ mode: "open" })
  }

  setConfig({ cameras }) {
    if (!Array.isArray(cameras) || !cameras.length ||
        cameras.some(camera => !/^camera\.[a-z0-9_]+$/.test(camera?.entity ?? "")) ||
        new Set(cameras.map(camera => camera.entity)).size !== cameras.length) {
      throw new Error("请为每个镜头填写不同的 camera 实体 ID")
    }
    this.started = false
    this.selectedTile = null
    this.stopRequests()
    for (const tile of this.tiles ?? []) clearTimeout(tile.feedbackTimer)
    this.setLive(false)
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; max-width: 1440px; margin: auto; padding: 16px; box-sizing: border-box; }
        header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
          gap: 12px; padding: 0 0 16px; }
        h1 { font-size: clamp(20px, 3vw, 28px); margin: 0; font-weight: 650; }
        .actions { display: flex; gap: 8px; }
        .control { font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
          border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 12px; cursor: pointer; }
        .control[aria-pressed="true"] { color: var(--text-primary-color); background: var(--primary-color); }
        .order-panel { margin: 0 0 16px; padding: 12px; border: 1px solid var(--divider-color);
          border-radius: 12px; background: var(--card-background-color); }
        .order-list { margin: 0 0 12px; padding-inline-start: 24px; }
        .order-list li { padding: 5px 0; }
        .order-row { display: flex; align-items: center; gap: 8px; }
        .order-row span { flex: 1; }
        .order-list li[draggable="true"] { cursor: grab; }
        .order-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
        .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        article { position: relative; min-width: 0; background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color); border-radius: 12px; overflow: hidden; }
        article[data-selected] { border-color: var(--primary-color); box-shadow: 0 0 0 1px var(--primary-color); }
        .media { position: relative; }
        .capture-player { position: absolute; inset: 0; opacity: 0; pointer-events: none; overflow: hidden; }
        .stream { position: relative; aspect-ratio: 16 / 9; background: var(--secondary-background-color); }
        .stream-loading { position: absolute; inset: 0; z-index: 1; display: grid; place-content: center;
          justify-items: center; gap: 10px; pointer-events: none; color: white; background: rgba(0,0,0,0.65); }
        .stream-spinner { width: 22px; height: 22px; border: 2px solid rgba(255,255,255,0.35);
          border-top-color: white; border-radius: 50%; animation: stream-spin 0.85s linear infinite; }
        @keyframes stream-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) { .stream-spinner { animation: none; } }
        .image { display: grid; place-items: center; width: 100%; aspect-ratio: 16 / 9; position: relative;
          border: 0; padding: 0; background: var(--secondary-background-color); color: var(--secondary-text-color);
          cursor: pointer; font: inherit; overflow: hidden; }
        .image img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
        .image span { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 8px; }
        footer { display: flex; gap: 4px 12px; flex-wrap: wrap; justify-content: space-between;
          align-items: center; padding: 8px 12px; }
        footer strong { font-size: 14px; font-weight: 600; }
        footer small { color: var(--secondary-text-color); font-size: 12px; }
        .live-meta { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
        .live-meta small + small::before { content: "·"; padding-inline-end: 5px; }
        .tile-actions { position: absolute; inset: 0; z-index: 2; pointer-events: none; }
        .media-controls { position: absolute; display: flex; align-items: center; gap: 5px; }
        .media-tools { top: 8px; right: 8px; }
        .media-playback { bottom: 8px; left: 8px; }
        .tile-actions button { opacity: 0; pointer-events: none; display: inline-grid; place-items: center;
          width: 38px; min-height: 38px; padding: 0; border-radius: 8px; color: white;
          background: transparent; border: 0; }
        .tile-actions .audio-level { opacity: 0; }
        article[data-selected] .tile-actions :is(button, .audio-level),
        article:has(:focus-visible) .tile-actions :is(button, .audio-level),
        .tile-actions .night-light[aria-pressed="true"] { opacity: 1; pointer-events: auto; }
        @media (hover: hover) {
          article:hover .tile-actions :is(button, .audio-level) { opacity: 1; pointer-events: auto; }
        }
        .tile-actions .night-light[aria-pressed="true"] { color: #ffd174; }
        .tile-actions button:hover { background: rgba(255,255,255,0.16); }
        .tile-actions button:focus-visible { outline: 2px solid white; outline-offset: -2px; }
        .tile-actions button[aria-pressed="true"] { color: #a8daff; }
        .tile-actions ha-icon { --mdc-icon-size: 19px; filter: drop-shadow(0 1px 2px rgba(0,0,0,0.9)); }
        .audio-level { width: 34px; height: 8px; accent-color: var(--primary-color); }
        .tile-feedback { color: var(--secondary-text-color); font-size: 12px; }
        .tile-feedback:empty, .refresh-status:empty { display: none; }
        .refresh-status { margin: 0 0 12px; color: var(--secondary-text-color); }
        .control:disabled { opacity: 0.55; cursor: default; }
        [hidden] { display: none !important; }
        @media (min-width: 1050px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
        @media (max-width: 540px) {
          :host { padding: 8px; } .grid { gap: 6px; } article { border-radius: 8px; }
          footer { padding: 6px; } footer strong { font-size: 12px; }
          .media-tools { top: 5px; right: 5px; }
          .media-playback { bottom: 5px; left: 5px; }
          .tile-actions button { width: 30px; min-height: 30px; }
          .tile-actions ha-icon { --mdc-icon-size: 16px; }
          .control { padding: 6px 8px; font-size: 12px; } .image { font-size: 12px; }
        }
      </style>
      <header><h1>摄像头总览</h1><div class="actions">
        <button class="control refresh">刷新封面</button>
        <button class="control live" aria-pressed="false">开启同屏直播</button>
        <button class="control order" aria-expanded="false" hidden>排序</button>
      </div></header>
      <section class="order-panel" aria-label="镜头排序" hidden>
        <ol class="order-list"></ol>
        <div class="order-footer">
          <button class="control order-save" type="button">保存顺序</button>
          <button class="control order-cancel" type="button">取消</button>
        </div>
      </section>
      <div class="order-status" role="status"></div>
      <div class="refresh-status" role="status"></div>
      <div class="grid"></div>`
    this.live = false
    this.liveButton = this.shadowRoot.querySelector(".live")
    this.refreshButton = this.shadowRoot.querySelector(".refresh")
    this.orderDraft = null
    this.orderSaving = false
    this.orderButton = this.shadowRoot.querySelector(".order")
    this.orderPanel = this.shadowRoot.querySelector(".order-panel")
    this.orderList = this.shadowRoot.querySelector(".order-list")
    this.orderStatus = this.shadowRoot.querySelector(".order-status")
    this.orderSave = this.shadowRoot.querySelector(".order-save")
    this.orderCancel = this.shadowRoot.querySelector(".order-cancel")
    this.orderButton.onclick = () => this.toggleOrder()
    this.orderCancel.onclick = () => this.toggleOrder()
    this.orderSave.onclick = () => this.saveOrder()
    this.tiles = cameras.map(config => {
      const camera = { ...config, name: config.name ?? config.entity }
      const article = document.createElement("article")
      article.innerHTML = `
        <div class="media"><button type="button" class="image"><img hidden alt="">
          <span><ha-icon icon="mdi:cctv"></ha-icon><em>正在获取预览</em></span>
        </button><div class="stream" hidden>
          <div class="stream-loading" role="status" hidden>
            <span class="stream-spinner" aria-hidden="true"></span><span>正在连接直播…</span>
          </div>
        </div>
        <div class="tile-actions">
          <div class="media-controls media-tools">
          <button class="control take-photo" type="button" title="拍照" aria-label="拍照" hidden>
            <ha-icon icon="mdi:camera" aria-hidden="true"></ha-icon>
          </button>
          <button class="control defence" type="button" title="布防设置" aria-label="布防设置" hidden>
            <ha-icon icon="mdi:shield-outline" aria-hidden="true"></ha-icon>
          </button>
          <button class="control night-light" type="button" title="开灯" aria-label="开灯" hidden>
            <ha-icon icon="mdi:lightbulb-on-outline" aria-hidden="true"></ha-icon>
          </button>
          </div>
          <div class="media-controls media-playback">
          <button class="control play-stream" type="button" title="播放直播" aria-label="播放直播">
            <ha-icon icon="mdi:play" aria-hidden="true"></ha-icon>
          </button>
          <button class="control sound" type="button" title="开启声音" aria-label="开启声音" hidden>
            <ha-icon icon="mdi:volume-off" aria-hidden="true"></ha-icon>
          </button>
          <progress class="audio-level" aria-label="直播声音电平" max="1" value="0" hidden></progress>
          </div>
        </div></div>
        <footer><strong></strong><small class="capture-status"></small>
          <span class="live-meta" hidden><small>直播模式</small><small class="night-mode"></small></span>
          <small class="tile-feedback" role="status"></small>
        </footer>`
      const button = article.querySelector(".image")
      button.setAttribute("aria-label", `选择镜头 · ${camera.name}`)
      button.setAttribute("aria-pressed", "false")
      const actions = article.querySelector(".tile-actions")
      const photo = actions.querySelector(".take-photo")
      const defence = actions.querySelector(".defence")
      const nightLight = actions.querySelector(".night-light")
      const play = actions.querySelector(".play-stream")
      const sound = actions.querySelector(".sound")
      const audioLevel = actions.querySelector(".audio-level")
      const feedback = article.querySelector(".tile-feedback")
      const tile = { camera, article, photo, defence, nightLight, play, sound, audioLevel, feedback, button,
        image: article.querySelector("img"), placeholder: article.querySelector(".image span"),
        message: article.querySelector("em"), stream: article.querySelector(".stream"),
        loading: article.querySelector(".stream-loading"),
        status: article.querySelector(".capture-status"), liveMeta: article.querySelector(".live-meta"),
        nightMode: article.querySelector(".night-mode") }
      article.querySelector(".media").addEventListener("click", event => {
        if (!event.composedPath().includes(actions)) this.selectCamera(tile)
      }, { capture: true })
      play.onclick = () => this.setTileLive(tile, !tile.liveToken)
      sound.onclick = () => this.toggleSound(tile)
      photo.setAttribute("aria-label", `拍照 · ${camera.name}`)
      defence.setAttribute("aria-label", `布防设置 · ${camera.name}`)
      photo.onclick = () => this.takePhoto(tile)
      nightLight.onclick = () => this.toggleNightLight(tile)
      defence.onclick = () => this.dispatchEvent(new CustomEvent("hass-more-info", {
        detail: { entityId: camera.defence_entity }, bubbles: true, composed: true
      }))
      article.querySelector("strong").textContent = camera.name
      this.shadowRoot.querySelector(".grid").append(article)
      return tile
    })
    this.refreshStatus = this.shadowRoot.querySelector(".refresh-status")
    this.refreshButton.onclick = () => this.captureCovers()
    this.liveButton.onclick = () => this.setLive(!this.live, true)
    this.updateControls()
    this.start()
  }

  selectCamera(tile) {
    const previous = this.selectedTile
    previous?.article.removeAttribute("data-selected")
    previous?.button.setAttribute("aria-pressed", "false")
    this.selectedTile = previous === tile ? null : tile
    if (this.selectedTile) {
      tile.article.setAttribute("data-selected", "")
      tile.button.setAttribute("aria-pressed", "true")
    }
  }

  toggleOrder() {
    if (this.orderSaving) return
    this.orderDraft = this.orderDraft ? null : [...this.tiles]
    this.orderStatus.textContent = ""
    this.orderPanel.hidden = !this.orderDraft
    this.orderButton.disabled = false
    this.orderButton.setAttribute("aria-expanded", String(Boolean(this.orderDraft)))
    if (this.orderDraft) this.renderOrder()
    else this.orderButton.focus()
  }

  renderOrder(focusTile, focusDirection) {
    const draft = this.orderDraft
    this.orderList.replaceChildren()
    this.orderSave.disabled = this.orderSaving || draft.every((tile, index) => tile === this.tiles[index])
    this.orderCancel.disabled = this.orderSaving
    this.orderButton.disabled = this.orderSaving
    draft.forEach((tile, index) => {
      const item = document.createElement("li")
      const row = document.createElement("div")
      row.className = "order-row"
      const name = document.createElement("span")
      name.textContent = tile.camera.name
      row.append(name)
      item.draggable = !this.orderSaving
      item.ondragstart = event => {
        event.dataTransfer.setData("text/plain", tile.camera.entity)
        event.dataTransfer.effectAllowed = "move"
      }
      item.ondragover = event => {
        if (!this.orderSaving) event.preventDefault()
      }
      item.ondrop = event => {
        event.preventDefault()
        const entity = event.dataTransfer.getData("text/plain")
        this.moveOrder(draft.findIndex(entry => entry.camera.entity === entity), index)
      }
      const buttons = [-1, 1].map(direction => {
        const button = document.createElement("button")
        button.type = "button"
        button.className = "control"
        button.textContent = direction < 0 ? "上移" : "下移"
        button.setAttribute("aria-label", `${tile.camera.name} · ${button.textContent}`)
        const target = index + direction
        button.disabled = this.orderSaving || target < 0 || target >= draft.length
        button.onclick = () => this.moveOrder(index, target, direction)
        return button
      })
      row.append(...buttons)
      item.append(row)
      this.orderList.append(item)
      if (tile === focusTile) {
        const preferred = buttons[focusDirection < 0 ? 0 : 1]
        const focus = preferred.disabled ? buttons.find(button => !button.disabled) : preferred
        focus?.focus()
      }
    })
  }

  moveOrder(from, to, focusDirection) {
    if (this.orderSaving || from < 0 || from === to) return
    const [tile] = this.orderDraft.splice(from, 1)
    this.orderDraft.splice(to, 0, tile)
    this.renderOrder(focusDirection ? tile : undefined, focusDirection)
  }

  async saveOrder() {
    if (this.orderSave.disabled) return
    const draft = this.orderDraft
    const [path, viewPath] = location.pathname.split("/").filter(Boolean)
    const dashboardPath = path === "lovelace" ? null : path
    this.orderSaving = true
    this.orderStatus.textContent = "正在保存顺序"
    this.renderOrder()
    try {
      const config = await this._hass.callWS({ type: "lovelace/config", url_path: dashboardPath })
      const view = viewPath ? config.views.find(view => view.path === viewPath) : config.views[0]
      const cards = view?.cards?.filter(card => card.type === "custom:ezviz-camera-overview") ?? []
      const card = cards.length === 1 ? cards[0] : null
      const latest = card?.cameras
      if (!Array.isArray(latest) || latest.length !== this.tiles.length ||
          !latest.every((camera, index) => camera.entity === this.tiles[index].camera.entity)) {
        this.orderStatus.textContent = "仪表板的镜头顺序已更新。请刷新页面后再排序。"
        return
      }
      const cameras = new Map(latest.map(camera => [camera.entity, camera]))
      card.cameras = draft.map(tile => cameras.get(tile.camera.entity))
      await this._hass.callWS({ type: "lovelace/config/save", url_path: dashboardPath, config })
      if (this.orderDraft !== draft) return
      this.tiles = draft
      const grid = this.shadowRoot.querySelector(".grid")
      for (const tile of draft) {
        if (grid.moveBefore) grid.moveBefore(tile.article, null)
        else grid.append(tile.article)
      }
      this.orderSaving = false
      this.toggleOrder()
      this.orderStatus.textContent = "已保存镜头顺序"
    } catch {
      this.orderStatus.textContent = "保存失败，请重试。"
    } finally {
      this.orderSaving = false
      if (this.orderDraft === draft) this.renderOrder()
    }
  }

  set hass(hass) {
    this._hass = hass
    for (const tile of this.tiles ?? []) if (tile.player) tile.player.hass = hass
    this.updateControls()
    this.start()
    if (this.started) this.refresh()
  }

  connectedCallback() {
    this.start()
  }

  disconnectedCallback() {
    this.audioContext?.close().catch(() => {})
    this.audioContext = null
    clearTimeout(this.refreshStatusTimer)
    this.started = false
    this.stopRequests()
    for (const tile of this.tiles ?? []) clearTimeout(tile.feedbackTimer)
    this.setLive(false)
  }

  stopRequests() {
    this.request?.abort()
    this.request = null
    for (const tile of this.tiles ?? []) {
      if (tile.imageUrl) URL.revokeObjectURL(tile.imageUrl)
      tile.imageUrl = null
      tile.attemptVersion = null
      tile.image.removeAttribute("src")
      tile.image.hidden = true
      tile.placeholder.hidden = false
    }
  }

  start() {
    if (this.started || !this.isConnected || !this.tiles || !this._hass?.states) return
    this.started = true
    this.refresh()
  }

  showFeedback(tile, message, duration = 0) {
    clearTimeout(tile.feedbackTimer)
    tile.feedback.textContent = message
    if (duration) {
      tile.feedbackTimer = setTimeout(() => {
        tile.feedback.textContent = ""
      }, duration)
    }
  }

  updateControls() {
    if (this.orderButton) this.orderButton.hidden = !this._hass?.user?.is_admin
    const states = this._hass?.states
    for (const tile of this.tiles ?? []) {
      const camera = states?.[tile.camera.entity]
      const alarm = states?.[tile.camera.defence_entity]
      const playing = Boolean(tile.liveToken)
      tile.photo.hidden = !camera
      tile.photo.disabled = Boolean(tile.takingPhoto) || camera?.state === "unavailable"
      tile.defence.hidden = !alarm
      tile.defence.disabled = alarm?.state === "unavailable"
      const nightMode = states?.[tile.camera.night_vision_entity]?.state
      const nightLabel = NIGHT_VISION_LABELS[nightMode]
      tile.status.hidden = playing
      tile.liveMeta.hidden = !playing
      tile.nightMode.textContent = nightLabel || "夜视状态不可用"
      tile.nightLight.hidden = !tile.camera.night_vision_entity
      tile.nightLight.disabled = Boolean(tile.changingNightMode) || !nightLabel
      tile.play.title = playing ? "返回静止画面" : "播放直播"
      tile.play.setAttribute("aria-label", `${tile.play.title} · ${tile.camera.name}`)
      tile.play.firstElementChild.setAttribute("icon", playing ? "mdi:pause" : "mdi:play")
      tile.sound.hidden = !playing
      tile.sound.disabled = !tile.video
      tile.sound.title = tile.soundOn ? "静音" : "开启声音"
      tile.sound.setAttribute("aria-label", `${tile.sound.title} · ${tile.camera.name}`)
      tile.sound.setAttribute("aria-pressed", String(Boolean(tile.soundOn)))
      tile.sound.firstElementChild.setAttribute("icon", tile.soundOn ? "mdi:volume-high" : "mdi:volume-off")
      tile.audioLevel.hidden = !tile.soundOn || !tile.audioMonitor
      const lightOn = nightMode === "full_color"
      tile.nightLight.setAttribute("aria-pressed", String(lightOn))
      tile.nightLight.title = lightOn ? "关灯（切回智能夜视）" : "开灯（切到全彩夜视）"
      tile.nightLight.firstElementChild.setAttribute("icon", lightOn ? "mdi:lightbulb-off-outline" : "mdi:lightbulb-on-outline")
      tile.nightLight.setAttribute("aria-label",
        `${nightMode === "full_color" ? "切回智能夜视" : "切到全彩夜视"} · ${tile.camera.name}`)
    }
  }

  async toggleNightLight(tile) {
    if (tile.nightLight.disabled || tile.nightLight.hidden) return
    const entity = tile.camera.night_vision_entity
    const option = this._hass.states[entity].state === "full_color" ? "smart" : "full_color"
    tile.changingNightMode = true
    this.showFeedback(tile, "正在切换夜视")
    this.updateControls()
    try {
      await this._hass.callService("select", "select_option", { entity_id: entity, option })
      this.showFeedback(tile, "")
      if (!tile.liveToken && !this.capturingCovers) {
        await new Promise(resolve => setTimeout(resolve, 800))
        if (tile.article.isConnected && !tile.liveToken && !this.capturingCovers) {
          await this.captureCover(tile).catch(() => {
            this.showFeedback(tile, "已切换夜视，暂时保留旧封面", 8000)
          })
        }
      }
    } catch {
      this.showFeedback(tile, "夜视切换失败", 8000)
    } finally {
      tile.changingNightMode = false
      this.updateControls()
    }
  }

  async takePhoto(tile) {
    if (tile.photo.disabled || tile.photo.hidden) return
    tile.takingPhoto = true
    this.showFeedback(tile, "拍照中")
    this.updateControls()
    try {
      const liveFrame = this.hasVideoFrame(tile.video) ? tile.video : null
      const image = liveFrame ? await this.frameBlob(liveFrame) : await this.captureStillFrame(tile)
      const photo = image ?? await this.readCoverPhoto(tile)
      const url = URL.createObjectURL(photo)
      const download = document.createElement("a")
      download.href = url
      download.download = `${tile.camera.entity.split(".")[1]}-${new Date().toISOString().replace(/:/g, "-")}.jpg`
      download.click()
      setTimeout(() => URL.revokeObjectURL(url), 30_000)
      this.showFeedback(tile, "已提交照片下载", 4000)
    } catch {
      this.showFeedback(tile, "拍照失败", 8000)
    } finally {
      tile.takingPhoto = false
      this.updateControls()
      this.refresh()
    }
  }

  hasVideoFrame(video) {
    return video?.videoWidth > 0 && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
  }

  async frameBlob(frame) {
    const canvas = document.createElement("canvas")
    canvas.width = frame.videoWidth ?? frame.naturalWidth
    canvas.height = frame.videoHeight ?? frame.naturalHeight
    canvas.getContext("2d").drawImage(frame, 0, 0)
    return new Promise((resolve, reject) => canvas.toBlob(blob => {
      if (blob) resolve(blob)
      else reject(new Error("Could not encode camera frame"))
    }, "image/jpeg", 0.92))
  }

  readCameraCover(tile, signal = AbortSignal.timeout(12_000)) {
    const headers = { Authorization: `Bearer ${this._hass.auth.accessToken}` }
    return this.loadCover(`/api/camera_proxy/${encodeURIComponent(tile.camera.entity)}`, headers, signal)
  }

  async readCoverPhoto(tile) {
    const url = await this.readCameraCover(tile)
    try {
      const image = new Image()
      image.src = url
      await image.decode()
      return await this.frameBlob(image)
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  async captureStillFrame(tile) {
    if (tile.liveToken) return null
    const container = document.createElement("div")
    container.className = "capture-player"
    container.inert = true
    container.setAttribute("aria-hidden", "true")
    tile.article.append(container)
    try {
      const player = await this.createCameraPlayer(tile)
      if (!container.isConnected || tile.liveToken) return null
      container.append(player)
      const deadline = performance.now() + 12_000
      while (container.isConnected && performance.now() < deadline) {
        player.hass = this._hass
        const video = tile.video || this.findVideo(container)
        if (video && video !== tile.video) video.muted = true
        if (this.hasVideoFrame(video)) return await this.frameBlob(video)
        await new Promise(resolve => setTimeout(resolve, 150))
      }
    } catch {
      // Fall back to the HA camera image if the browser cannot decode a frame.
    } finally {
      container.remove()
    }
    return null
  }

  showNewCover(tile, url, timestamp, label = "封面读取") {
    if (tile.imageUrl && Date.parse(tile.coverTimestamp) > Date.parse(timestamp)) {
      URL.revokeObjectURL(url)
      return
    }
    if (tile.imageUrl) URL.revokeObjectURL(tile.imageUrl)
    tile.imageUrl = url
    tile.coverTimestamp = timestamp
    tile.image.src = url
    tile.image.hidden = false
    tile.placeholder.hidden = true
    const time = new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    tile.status.textContent = `${label} ${time}`
  }

  async captureCover(tile) {
    if (tile.takingPhoto) throw new Error("Manual capture in progress")
    const video = tile.video
    let url
    if (this.hasVideoFrame(video)) {
      url = URL.createObjectURL(await this.frameBlob(video))
    } else {
      try {
        url = await this.readCameraCover(tile)
      } catch {
        const image = await this.captureStillFrame(tile)
        if (!image) throw new Error("Camera returned no cover")
        url = URL.createObjectURL(image)
      }
    }
    if (tile.article.isConnected) this.showNewCover(tile, url, new Date().toISOString())
    else URL.revokeObjectURL(url)
  }

  async captureCovers() {
    if (this.capturingCovers || !this.isConnected) return
    this.capturingCovers = true
    const tiles = [...this.tiles]
    const status = this.refreshStatus
    this.refreshButton.disabled = true
    status.textContent = "正在重新抓拍封面…"
    this.request?.abort()
    let success = 0
    try {
      for (const tile of tiles) {
        if (!this.isConnected || !this.tiles.includes(tile)) break
        try {
          await this.captureCover(tile)
          success++
        } catch {
          // Preserve this tile's previous cover and continue with the next camera.
        }
      }
      const failure = tiles.length - success
      status.textContent = failure ? `已更新 ${success}/${tiles.length} 路，${failure} 路暂时保留旧封面` :
        `已更新 ${success} 路封面`
      clearTimeout(this.refreshStatusTimer)
      this.refreshStatusTimer = setTimeout(() => { status.textContent = "" }, failure ? 8000 : 4000)
    } finally {
      this.capturingCovers = false
      this.refreshButton.disabled = Boolean(this.request)
    }
  }

  async loadCover(endpoint, headers, signal) {
    const response = await fetch(endpoint, { headers, signal, cache: "no-store" })
    if (!response.ok) throw new Error("Snapshot unavailable")
    const url = URL.createObjectURL(await response.blob())
    try {
      const image = new Image()
      image.src = url
      await image.decode()
      signal.throwIfAborted()

      // Some EZVIZ captures decode successfully with a flat gray or black lower region.
      const sample = document.createElement("canvas")
      sample.width = 32
      sample.height = 24
      const context = sample.getContext("2d", { willReadFrequently: true })
      context.drawImage(image, 0, 0, sample.width, sample.height)
      const { data } = context.getImageData(0, 0, sample.width, sample.height)
      const end = data.length - 4
      const dark = Math.max(data[end], data[end + 1], data[end + 2]) <= 128
      const flat = (start, stop) => {
        for (let i = start; i < stop; i += 4) {
          for (let channel = 0; channel < 3; channel++) {
            if (Math.abs(data[i + channel] - data[end + channel]) > 2) return false
          }
        }
        return true
      }
      const row = sample.width * 4
      if (dark && flat(10 * row, data.length) && !flat(0, 10 * row)) {
        throw new Error("Incomplete snapshot")
      }
      return url
    } catch (error) {
      URL.revokeObjectURL(url)
      throw error
    }
  }

  async refresh() {
    if (!this.isConnected || !this._hass?.states || this.request || this.capturingCovers) return
    const request = this.request = new AbortController()
    this.refreshButton.disabled = true
    const headers = { Authorization: `Bearer ${this._hass.auth.accessToken}` }
    try {
      for (const tile of this.tiles) {
        if (request.signal.aborted) break
        if (tile.liveToken) continue
        const imageEntity = this._hass.states[tile.camera.image_entity]
        const timestamp = imageEntity?.state
        const hasImage = Number.isFinite(Date.parse(timestamp))
        const version = hasImage ? timestamp : "camera"
        if (tile.attemptVersion === version) continue
        tile.attemptVersion = version
        const signal = AbortSignal.any([request.signal, AbortSignal.timeout(12_000)])
        try {
          let url
          let isCapture = hasImage
          if (isCapture) {
            const endpoint = `/api/image_proxy/${encodeURIComponent(tile.camera.image_entity)}`
            url = await this.loadCover(endpoint, headers, signal).catch(() => null)
            isCapture = Boolean(url)
          }
          url ??= await this.readCameraCover(tile, signal)
          if (request.signal.aborted || tile.liveToken) URL.revokeObjectURL(url)
          else this.showNewCover(tile, url, isCapture ? timestamp : new Date().toISOString(),
            isCapture ? "抓拍" : "封面读取")
        } catch {
          if (request.signal.aborted) {
            tile.attemptVersion = null
            break
          }
          tile.message.textContent = "暂无静态预览 · 可播放直播"
          tile.status.textContent = tile.imageUrl ? "保留上一张" : "封面不可用"
        }
      }
    } finally {
      if (this.request === request) {
        this.request = null
        this.refreshButton.disabled = Boolean(this.capturingCovers)
      }
    }
  }

  findVideo(root) {
    const video = root.querySelector("video")
    if (video) return video
    for (const element of root.querySelectorAll("*")) {
      if (!element.shadowRoot) continue
      const nested = this.findVideo(element.shadowRoot)
      if (nested) return nested
    }
    return null
  }

  async toggleSound(tile) {
    if (!tile.video) return
    tile.soundOn = !tile.soundOn
    tile.video.muted = !tile.soundOn
    if (tile.soundOn && window.AudioContext) {
      try {
        this.audioContext ??= new AudioContext()
        await this.audioContext.resume()
      } catch {
        // Playback can still use the media element without an audio meter.
      }
    }
    this.updateControls()
  }

  clearCapturedAudio(tile) {
    for (const track of tile.capturedAudio?.getTracks() ?? []) track.stop()
    tile.capturedAudio = null
    tile.captureAttempted = false
  }

  audioTrack(tile, video) {
    if (!tile.soundOn || !video) return null
    if (video.srcObject?.getAudioTracks) {
      return video.srcObject.getAudioTracks().find(track => track.readyState === "live")
    }
    if (!tile.captureAttempted && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
      tile.captureAttempted = true
      try {
        tile.capturedAudio = video.captureStream?.()
      } catch {
        // Some browsers do not expose audio tracks for this kind of player.
      }
    }
    return tile.capturedAudio?.getAudioTracks().find(track => track.readyState === "live")
  }

  monitorTile(tile, token) {
    const started = performance.now()
    const check = () => {
      if (tile.liveToken !== token) return
      const video = this.findVideo(tile.stream)
      const videoChanged = tile.video !== video
      if (videoChanged) this.clearCapturedAudio(tile)
      tile.video = video
      if (video) {
        video.muted = !tile.soundOn
        if (video.videoWidth && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
          tile.loading.hidden = true
        }
      }
      if (performance.now() - started >= 15_000) tile.loading.hidden = true
      const track = this.audioTrack(tile, video)
      if ((track ?? null) !== (tile.audioMonitor?.track ?? null)) {
        tile.audioMonitor?.source.disconnect()
        tile.audioMonitor = null
        if (track && this.audioContext?.state === "running") {
          const source = this.audioContext.createMediaStreamSource(new MediaStream([track]))
          const analyser = this.audioContext.createAnalyser()
          analyser.fftSize = 256
          source.connect(analyser)
          tile.audioMonitor = { track, source, analyser, samples: new Float32Array(analyser.fftSize) }
        }
        this.updateControls()
      } else if (videoChanged) {
        this.updateControls()
      }
      if (tile.audioMonitor) {
        const { analyser, samples } = tile.audioMonitor
        analyser.getFloatTimeDomainData(samples)
        let energy = 0
        for (const sample of samples) energy += sample * sample
        tile.audioLevel.value = Math.min(1, Math.sqrt(energy / samples.length) * 5)
      }
      tile.loadingTimer = setTimeout(check, 150)
    }
    check()
  }

  async createCameraPlayer(tile) {
    const helpers = await window.loadCardHelpers()
    const player = helpers.createCardElement({
      type: "picture-entity", entity: tile.camera.entity, camera_view: "live",
      show_name: false, show_state: false, aspect_ratio: "16:9",
      tap_action: { action: "none" }
    })
    player.hass = this._hass
    return player
  }

  async setTileLive(tile, active, freeze = true) {
    if (active === Boolean(tile.liveToken)) return
    if (!active) {
      const timestamp = new Date().toISOString()
      const lastFrame = freeze && this.hasVideoFrame(tile.video)
        ? this.frameBlob(tile.video).catch(() => null) : null
      tile.liveToken = null
      clearTimeout(tile.loadingTimer)
      tile.audioMonitor?.source.disconnect()
      tile.audioMonitor = null
      this.clearCapturedAudio(tile)
      tile.soundOn = false
      tile.video = null
      tile.player?.remove()
      tile.player = null
      tile.loading.hidden = true
      tile.stream.hidden = true
      tile.button.hidden = false
      this.updateControls()
      const image = await lastFrame
      if (image && tile.article.isConnected) {
        this.showNewCover(tile, URL.createObjectURL(image), timestamp, "直播最后一帧")
      } else if (this.started) {
        this.refresh()
      }
      return
    }
    const token = tile.liveToken = Symbol()
    tile.button.hidden = true
    tile.stream.hidden = false
    tile.loading.hidden = false
    this.updateControls()
    this.monitorTile(tile, token)
    try {
      const player = await this.createCameraPlayer(tile)
      if (tile.liveToken !== token || !this.isConnected) return
      tile.player = player
      tile.stream.prepend(player)
    } catch {
      if (tile.liveToken === token) {
        this.setTileLive(tile, false, false)
        this.showFeedback(tile, "无法打开直播", 8000)
      }
    }
  }

  setLive(active, freeze = false) {
    this.live = active
    if (!this.liveButton) return
    this.liveButton.textContent = active ? "关闭同屏直播" : "开启同屏直播"
    this.liveButton.setAttribute("aria-pressed", String(active))
    for (const tile of this.tiles) this.setTileLive(tile, active, freeze)
  }

}

if (!customElements.get("ezviz-camera-overview")) {
  customElements.define("ezviz-camera-overview", EzvizCameraOverview)
}
