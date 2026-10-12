import QtQuick
import Quickshell
import Quickshell.Io

// Headless half of OmaiPhone. One instance for the whole shell: it starts the
// two background programs and holds what the bar icon, the panel and the
// Messages card show.
//
//   backend/omaiphone-ctl daemon    calls (HFP via PipeWire), call audio, ringing,
//                                   iPhone notifications and music (BlueZ GATT)
//   backend/omaiphone-ctl watch     one JSON status line per change
//   backend/texts/messagesd.py      texts (MAP via obexd)
//
// All three end with the shell (setpriv --pdeathsig). Every action is the
// same CLI with an argument list; nothing goes through a shell.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  function fileUrl(relative) {
    var url = Qt.resolvedUrl(relative).toString()
    if (url.indexOf("file://") === 0) url = url.substring(7)
    return decodeURIComponent(url)
  }

  readonly property string cli: fileUrl("../backend/omaiphone-ctl")
  readonly property string textsDaemon: fileUrl("../backend/texts/messagesd.py")

  // One-shot CLI calls get a hard time limit. The CLI caps what it prints.
  function ctl(seconds, args) {
    return ["timeout", "-k", "5", String(seconds), root.cli].concat(args)
  }

  // ---------------------------------------------------------------------------
  // Calls, contacts, notifications, music
  // ---------------------------------------------------------------------------

  property var status: null
  property var contactEntries: []         // flattened [{name, number}]
  property var historyEntries: []         // [{type, time, number, name}] newest first
  property string lastError: ""
  property real activeSince: 0
  property int elapsed: 0
  property string lastCallState: "idle"

  readonly property var phone: status ? status.phone : null
  readonly property var call: status ? status.call : null
  readonly property var caps: status && status.capabilities ? status.capabilities : ({})
  readonly property var audioInfo: status ? status.audio : null
  readonly property var settings: status && status.settings ? status.settings : ({})
  readonly property bool connected: !!(status && status.hfp)
  readonly property string callState: call ? String(call.state) : "idle"
  readonly property bool ringing: callState === "incoming" || callState === "waiting"
  readonly property bool inCall: call !== null && callState !== "disconnected"
  readonly property bool muted: !!(audioInfo && audioInfo.muted)
  readonly property int missedUnseen: status && status.missedUnseen ? status.missedUnseen : 0
  readonly property var battery: phone && phone.battery !== undefined && phone.battery !== null ? phone.battery : null

  readonly property var mirror: status && status.mirror ? status.mirror : null
  readonly property bool mirrorOn: !!(mirror && mirror.enabled)
  readonly property var inbox: mirror && mirror.notifications ? mirror.notifications : []
  readonly property var media: mirror && mirror.media ? mirror.media : null
  readonly property bool hasMedia: !!(media && media.available && (media.title || media.artist))

  // Bumped when a call starts ringing; the bar widget on the focused monitor opens.
  property int ringRevision: 0
  // Bumped to open the panel from a keybinding: the bar widget on the focused monitor opens.
  property int panelRevision: 0
  property string panelTab: ""

  // Actions run one after another so a quick second click is not lost.
  property var queue: []

  function run(args) {
    lastError = ""
    queue = queue.concat([args])
    pump()
  }

  function pump() {
    if (actionProcess.running || queue.length === 0) return
    // Contact sync and connecting can take a while over Bluetooth.
    actionProcess.command = root.ctl(120, queue[0])
    queue = queue.slice(1)
    actionProcess.running = true
  }

  function setSetting(key, on) { run(["setting", key, on ? "on" : "off"]) }
  function refreshContacts() { if (!contactsProcess.running) contactsProcess.running = true }
  function refreshHistory() { if (!historyProcess.running) historyProcess.running = true }
  function syncHistory() { if (!syncHistoryProcess.running) syncHistoryProcess.running = true }

  function applyStatus(line) {
    var text = String(line).trim()
    if (text === "") return
    var next
    try {
      next = JSON.parse(text)
    } catch (error) {
      return
    }
    var state = next.call ? String(next.call.state) : "idle"
    if (state === "active" && lastCallState !== "active") activeSince = Date.now()
    if (state !== "active" && state !== "held") activeSince = 0
    if ((state === "incoming" || state === "waiting") && lastCallState !== state) ringRevision += 1
    if (status && next.missedUnseen !== status.missedUnseen) refreshHistory()
    // Contacts arrive a moment after the phone connects (PBAP); reload once they exist.
    if (next.capabilities && next.capabilities.hasPhonebook && contactEntries.length === 0) refreshContacts()
    lastCallState = state
    status = next
  }

  function loadContacts(output) {
    var list = []
    try {
      var data = JSON.parse(String(output).trim() || "[]")
      for (var i = 0; i < data.length; i++)
        for (var j = 0; j < data[i].numbers.length; j++)
          list.push({ name: String(data[i].name), number: String(data[i].numbers[j]) })
    } catch (error) {
      list = []
    }
    contactEntries = list
  }

  function loadHistory(output) {
    try {
      var data = JSON.parse(String(output).trim() || "{}")
      historyEntries = data.entries || []
    } catch (error) {
      historyEntries = []
    }
  }

  Process {
    id: daemonProcess
    command: ["setpriv", "--pdeathsig", "TERM", root.cli, "daemon"]
    running: true
    // Its log is connection state only and is drained so the pipe never fills.
    stdout: SplitParser { onRead: function(line) {} }
    stderr: SplitParser { onRead: function(line) {} }
    onExited: daemonRestart.restart()
  }

  Timer {
    id: daemonRestart
    interval: 5000
    onTriggered: daemonProcess.running = true
  }

  Process {
    id: watchProcess
    command: [root.cli, "watch"]
    running: true
    stdout: SplitParser { onRead: function(line) { root.applyStatus(line) } }
    stderr: SplitParser { onRead: function(line) {} }
    onExited: watchRestart.restart()
  }

  Timer {
    id: watchRestart
    interval: 3000
    onTriggered: watchProcess.running = true
  }

  Process {
    id: actionProcess
    onExited: {
      if (!statusOnceProcess.running) statusOnceProcess.running = true
      root.pump()
    }
    stdout: SplitParser { onRead: function(line) {} }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text).trim()
        if (message !== "") root.lastError = message.replace(/^omaiphone(-ctl)?:\s*/, "")
      }
    }
  }

  // Mute and similar changes emit no D-Bus signal, so read the state once after every action.
  Process {
    id: statusOnceProcess
    command: root.ctl(15, ["status", "--json"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  Process {
    id: contactsProcess
    command: root.ctl(15, ["contacts", "list", "--json"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.loadContacts(text)
    }
  }

  Process {
    id: historyProcess
    command: root.ctl(15, ["history", "--json"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.loadHistory(text)
    }
  }

  Process {
    id: syncHistoryProcess
    command: root.ctl(120, ["history", "sync"])
    onExited: root.refreshHistory()
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.activeSince > 0
    triggeredOnStart: true
    onTriggered: root.elapsed = Math.max(0, Math.floor((Date.now() - root.activeSince) / 1000))
  }

  // ---------------------------------------------------------------------------
  // Texts (the Messages card)
  // ---------------------------------------------------------------------------

  property int unread: 0
  property string textState: "not_paired"
  property string detail: "Starting…"
  property string phoneName: ""
  property string phoneAddress: ""
  property string ownNumber: ""
  property string pairCode: ""
  property bool pairFallback: false
  property bool notifyBody: false
  property bool bluetoothReady: false
  property string textError: ""
  property int threadRevision: 0
  property int messageRevision: 0
  property int deviceRevision: 0
  property int contactRevision: 0
  property string activeThread: ""

  function send(command) {
    if (!bridge.running) return
    bridge.write(JSON.stringify(command) + "\n")
  }

  function refresh() { root.send({ "cmd": "status" }) }
  function setNumber(number) { root.send({ "cmd": "set-number", "number": number }) }
  function setNotifyBody(enabled) { root.send({ "cmd": "set-notify-body", "enabled": enabled }) }
  function scan() { root.send({ "cmd": "scan" }) }
  function reconnect() { root.send({ "cmd": "reconnect" }) }
  function pair(address) { root.send({ "cmd": "pair", "address": address, "explicit": false }) }
  function pairExplicit(address) { root.send({ "cmd": "pair", "address": address, "explicit": true }) }
  function confirm(accept) { root.send({ "cmd": "confirm", "accept": accept }) }
  function cancelPair() { root.send({ "cmd": "cancel" }) }
  function loadThreads() { root.send({ "cmd": "threads" }) }
  function loadMessages(thread) {
    root.activeThread = thread
    root.send({ "cmd": "messages", "thread": thread })
  }
  function clearMessages() {
    // A new person has no conversation yet; the card must not keep showing
    // somebody else's texts.
    root.activeThread = ""
    messageModel.clear()
  }
  function searchContacts(query) { root.send({ "cmd": "contacts", "query": query }) }
  function sendText(thread, address, body) {
    root.send({ "cmd": "send", "thread": thread, "address": address, "body": body })
  }
  function markRead(thread) { root.send({ "cmd": "mark-read", "thread": thread }) }

  // Open the Messages card, optionally straight at one person.
  function openMessages(address, label) {
    if (!root.shell || typeof root.shell.summon !== "function") return
    var id = (root.manifest && root.manifest.id) || "io.github.dankestrick.omaiphone"
    root.shell.summon(id, address ? JSON.stringify({ to: address, label: label || address }) : "{}")
  }

  function applyTextStatus(status) {
    if (!status) return
    root.textState = status.state || "not_paired"
    root.detail = status.detail || ""
    root.phoneName = status.phoneName || ""
    root.phoneAddress = status.phoneAddress || ""
    root.ownNumber = status.ownNumber || ""
    root.unread = Number(status.unread || 0)
    root.notifyBody = status.notifyBody === true
    root.pairFallback = status.pairFallback === true
    root.bluetoothReady = status.bluetoothReady === true
    if (root.textState !== "pairing") root.pairCode = ""
  }

  function fill(model, rows, fields) {
    model.clear()
    if (!rows) return
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i]
      var item = {}
      for (var f = 0; f < fields.length; f++) {
        var key = fields[f]
        var value = row[key]
        item[key] = value === undefined || value === null ? "" : value
      }
      model.append(item)
    }
  }

  function handleLine(line) {
    var text = String(line || "").trim()
    if (!text) return
    var event
    try {
      event = JSON.parse(text)
    } catch (ignore) {
      return
    }
    if (!event || !event.event) return
    if (event.event === "ready" || event.event === "status") {
      root.applyTextStatus(event.status)
    } else if (event.event === "pair-code") {
      root.pairCode = event.code || ""
      root.textState = "pairing"
    } else if (event.event === "error") {
      root.textError = event.message || "Something went wrong."
    } else if (event.event === "threads") {
      root.fill(threadModel, event.threads, ["key", "label", "preview", "unread", "timestamp"])
      root.threadRevision += 1
    } else if (event.event === "messages") {
      if (!root.activeThread || event.thread === root.activeThread) {
        root.fill(messageModel, event.messages, ["id", "thread", "label", "body", "direction", "timestamp", "read"])
        root.messageRevision += 1
      }
    } else if (event.event === "message") {
      root.loadThreads()
      if (root.activeThread && event.message && event.message.thread === root.activeThread)
        root.loadMessages(root.activeThread)
    } else if (event.event === "open-thread") {
      // openMessages takes the number or email. address_from_thread already stripped tel: or email:.
      root.openMessages(String(event.address || ""), String(event.label || ""))
    } else if (event.event === "devices") {
      root.fill(deviceModel, event.devices, ["address", "name", "paired", "connected"])
      root.deviceRevision += 1
    } else if (event.event === "contacts") {
      root.fill(contactModel, event.contacts, ["name", "address", "kind"])
      root.contactRevision += 1
    } else if (event.event === "sent") {
      root.loadThreads()
      if (root.activeThread) root.loadMessages(root.activeThread)
    }
  }

  ListModel { id: threadModel }
  ListModel { id: messageModel }
  ListModel { id: contactModel }
  ListModel { id: deviceModel }

  property alias threads: threadModel
  property alias messages: messageModel
  property alias contacts: contactModel
  property alias devices: deviceModel

  Process {
    id: bridge
    stdinEnabled: true
    // -B: no __pycache__ in the plugin folder (the shell reloads on every file change there).
    command: ["setpriv", "--pdeathsig", "TERM", "python3", "-B", "-u", root.textsDaemon]
    running: true
    stdout: SplitParser { onRead: function(data) { root.handleLine(data) } }
    // Stderr is connection state only; drained so the pipe cannot fill and stall the daemon.
    stderr: SplitParser { onRead: function(data) {} }
    onExited: bridgeRestart.restart()
  }

  Timer {
    id: bridgeRestart
    interval: 1500
    onTriggered: bridge.running = true
  }

  // omarchy-shell io.github.dankestrick.omaiphone.phone <panel|messages|answer|hangup|mute>
  IpcHandler {
    target: "io.github.dankestrick.omaiphone.phone"
    function panel(): void { root.panelTab = ""; root.panelRevision += 1 }
    function tab(name: string): void { root.panelTab = name; root.panelRevision += 1 }
    function messages(): void { root.openMessages("", "") }
    function answer(): void { root.run(["answer"]) }
    function hangup(): void { root.run(["hangup"]) }
    function mute(): void { root.run(["mute", "toggle"]) }
  }

  Component.onCompleted: {
    refreshContacts()
    refreshHistory()
  }

  Component.onDestruction: {
    daemonRestart.stop()
    watchRestart.stop()
    bridgeRestart.stop()
  }
}
