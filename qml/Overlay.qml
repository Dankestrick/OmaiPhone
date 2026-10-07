import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Centered terminal-style card. The dim layer behind it is the overlay;
// the card itself stays in the middle of the screen. Every message is
// plain text. Images are never drawn.
Item {
  id: root

  property string omarchyPath: ""
  property var shell: null
  property var manifest: null

  property bool opened: false
  property string screen: "setup"
  property int selected: 0
  property string filterText: ""
  property string draft: ""
  property bool composeFocus: false

  onComposeFocusChanged: {
    if (root.composeFocus)
      composer.forceActiveFocus()
    else
      keyCatcher.forceActiveFocus()
  }
  property bool helpOn: false
  property string toAddress: ""
  property string toLabel: ""
  property string lastOpened: ""
  property bool caretOn: true

  Timer {
    // A blinking mark at the end of the line, so it is obvious the card
    // is waiting for what you want to send.
    interval: 500
    repeat: true
    running: root.opened && root.composeFocus
    onTriggered: root.caretOn = !root.caretOn
  }

  readonly property var svc: (shell && shell.serviceFor)
    ? shell.serviceFor((manifest && manifest.id) || "io.github.dankestrick.omaiphone") : null

  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  property color selectedBackground: Color.accent
  property color selectedText: Color.background
  property color incomingBubble: Util.alpha(foreground, 0.12)
  property color muted: Color.muted
  property string fontFamily: Style.font.family
  readonly property int cornerRadius: Style.cornerRadius
  property int contentMargin: Style.spacing.panelPadding
  // A window in the middle, not a sheet that covers the desktop.
  property int cardWidth: Math.min(Style.space(760), Math.max(Style.space(520), panel.width * 0.52))
  property int cardHeight: Math.min(Style.space(480), Math.max(Style.space(340), panel.height * 0.52))

  function open(payloadJson) {
    root.opened = true
    root.helpOn = false
    root.composeFocus = false
    root.toAddress = ""
    root.toLabel = ""
    root.filterText = ""
    root.selected = 0
    root.followState(true)
    var payload = null
    try {
      payload = payloadJson ? JSON.parse(payloadJson) : null
    } catch (ignore) {
      payload = null
    }
    // A saved phone retries the message link. A missing phone starts a scan.
    if (root.svc && (root.svc.textState === "waiting_for_switches" || root.svc.textState === "phone_nearby"))
      root.svc.reconnect()
    else if (root.screen === "setup" && root.svc)
      root.svc.scan()
    Qt.callLater(function() {
      keyCatcher.forceActiveFocus()
      // Opened from the panel for one person: start typing to them right away.
      // This runs after the focus above so the reply line keeps the keyboard.
      if (payload && payload.to && root.screen === "inbox")
        root.startConversation(String(payload.to), String(payload.label || payload.to))
    })
  }

  function close() {
    root.opened = false
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "io.github.dankestrick.omaiphone")
  }

  function followState(initial) {
    if (!root.svc) {
      root.screen = "setup"
      return
    }
    if (root.svc.pairCode) {
      root.screen = "code"
      return
    }
    if (!initial && (root.screen === "contacts" || root.screen === "emoji"))
      return
    if (root.svc.textState === "waiting_for_switches" && root.screen !== "inbox") {
      root.screen = "checklist"
      return
    }
    if (root.svc.textState === "connected" && (initial || root.screen === "setup" || root.screen === "code" || root.screen === "checklist")) {
      root.screen = "inbox"
      root.rebuildShown()
      root.openSelectedThread()
      return
    }
    if (initial && root.svc.threads.count === 0)
      root.screen = "setup"
    root.rebuildShown()
  }

  function paneModel() {
    if (root.screen === "emoji")
      return emojiModel
    if (root.screen === "contacts" && root.svc)
      return root.svc.contacts
    return shownModel
  }

  function rebuildShown() {
    if (root.screen === "contacts" || root.screen === "emoji")
      return
    var keep = ""
    if (root.selected >= 0 && root.selected < shownModel.count) {
      var current = shownModel.get(root.selected)
      keep = current ? (current.key || current.address || "") : ""
    }
    shownModel.clear()
    if (!root.svc)
      return
    var source = root.screen === "setup" ? root.svc.devices : root.svc.threads
    var needle = root.filterText.toLowerCase()
    var restore = 0
    var rows = []
    for (var i = 0; i < source.count; i++) {
      var row = source.get(i)
      var hay = ((row.label || row.name || "") + " " + (row.preview || row.address || "")).toLowerCase()
      if (needle && hay.indexOf(needle) === -1)
        continue
      rows.push({
        key: row.key || "",
        label: row.label || row.name || "",
        preview: row.preview || "",
        unread: row.unread || 0,
        timestamp: row.timestamp || "",
        address: row.address || "",
        name: row.name || "",
        pending: 0
      })
    }
    // A person with no texts yet still needs a row, or the highlight
    // stays on whoever you were reading before.
    if (root.screen === "inbox" && root.toLabel && !root.listHasRecipient(rows)) {
      rows.unshift({
        key: "",
        label: root.toLabel,
        preview: "",
        unread: 0,
        timestamp: "",
        address: root.toAddress,
        name: root.toLabel,
        pending: 1
      })
    }
    // Unread conversations stay at the top. Read ones follow, newest first.
    if (root.screen === "inbox") {
      rows.sort(function(a, b) {
        var ap = a.pending ? 1 : 0
        var bp = b.pending ? 1 : 0
        if (ap !== bp)
          return bp - ap
        var au = a.unread > 0 ? 1 : 0
        var bu = b.unread > 0 ? 1 : 0
        if (au !== bu)
          return bu - au
        if (a.timestamp === b.timestamp)
          return 0
        return a.timestamp < b.timestamp ? 1 : -1
      })
    }
    var addressed = -1
    for (var n = 0; n < rows.length; n++) {
      if ((rows[n].key || rows[n].address || "") === keep)
        restore = shownModel.count
      if (root.toLabel && (rows[n].pending || root.samePerson(rows[n], root.toAddress, root.toLabel)))
        addressed = shownModel.count
      shownModel.append(rows[n])
    }
    if (addressed >= 0)
      restore = addressed
    root.selected = shownModel.count === 0 ? 0 : Math.min(restore, shownModel.count - 1)
  }

  function digitsOnly(value) {
    return String(value || "").replace(/\D/g, "")
  }

  // Same person if the name matches, or the phone numbers share an ending.
  // A UK 07… number and a +44… number are the same last digits.
  function samePerson(row, address, name) {
    if (!row)
      return false
    if (name && row.label && String(row.label) === String(name))
      return true
    var wanted = String(address || "").trim().toLowerCase()
    var key = String(row.key || "").toLowerCase()
    if (wanted && (key === wanted || key === "email:" + wanted || key === "tel:" + wanted))
      return true
    var wantDigits = root.digitsOnly(wanted)
    var keyDigits = root.digitsOnly(key)
    if (wantDigits.length >= 7 && keyDigits.length >= 7) {
      var n = Math.min(10, wantDigits.length, keyDigits.length)
      return wantDigits.slice(-n) === keyDigits.slice(-n)
    }
    return false
  }

  function listHasRecipient(rows) {
    for (var i = 0; i < rows.length; i++) {
      if (root.samePerson(rows[i], root.toAddress, root.toLabel))
        return true
    }
    return false
  }

  function recipientLabel() {
    if (root.toLabel)
      return root.toLabel
    if (root.screen !== "inbox" || root.selected < 0 || root.selected >= shownModel.count)
      return ""
    var row = shownModel.get(root.selected)
    return row ? (row.label || "") : ""
  }

  function cancelCompose() {
    var addressed = root.toAddress !== ""
    root.composeFocus = false
    root.toAddress = ""
    root.toLabel = ""
    if (addressed)
      root.rebuildShown()
  }

  function openSelectedThread() {
    if (root.screen !== "inbox" || !root.svc)
      return
    if (root.selected < 0 || root.selected >= shownModel.count)
      return
    var row = shownModel.get(root.selected)
    if (!row || !row.key || row.key === root.lastOpened)
      return
    root.lastOpened = row.key
    root.svc.loadMessages(row.key)
    root.svc.markRead(row.key)
  }

  function move(delta) {
    var model = root.paneModel()
    if (!model || model.count === 0)
      return
    root.selected = (root.selected + delta + model.count) % model.count
    if (leftList)
      leftList.positionViewAtIndex(root.selected, ListView.Contain)
    if (root.screen === "inbox")
      root.openSelectedThread()
  }

  function rowText(index) {
    var model = root.paneModel()
    if (!model || index < 0 || index >= model.count)
      return ""
    var row = model.get(index)
    if (root.screen === "setup")
      return (row.name || "Phone") + "  " + (row.address || "")
    if (root.screen === "contacts")
      return row.name || "Contact"
    if (root.screen === "emoji")
      return (row.emoji || "") + "  " + (row.keywords || "")
    return row.label || "Unknown"
  }

  function pairSelected(explicit) {
    if (!root.svc || root.selected < 0 || root.selected >= shownModel.count)
      return
    var row = shownModel.get(root.selected)
    if (!row || !row.address)
      return
    if (explicit)
      root.svc.pairExplicit(row.address)
    else
      root.svc.pair(row.address)
  }

  function pickContact() {
    if (!root.svc)
      return
    var model = root.svc.contacts
    if (root.selected < 0 || root.selected >= model.count)
      return
    var row = model.get(root.selected)
    root.startConversation(row.address || "", row.name || row.address || "Contact")
  }

  // Also used when the panel's Calls tab asks to text a contact.
  function startConversation(address, label) {
    root.toAddress = address
    root.toLabel = label
    root.screen = "inbox"
    root.filterText = ""
    root.rebuildShown()
    var chosen = root.selected >= 0 && root.selected < shownModel.count ? shownModel.get(root.selected) : null
    if (chosen && chosen.key) {
      root.lastOpened = ""
      root.openSelectedThread()
    } else if (root.svc) {
      root.svc.clearMessages()
      root.lastOpened = ""
    }
    if (leftList && shownModel.count > 0)
      leftList.positionViewAtIndex(root.selected, ListView.Contain)
    root.startReply()
  }

  function startReply() {
    // The hidden text field is what actually receives letters and paste.
    composer.clear()
    root.draft = ""
    root.caretOn = true
    root.composeFocus = true
  }

  function loadEmojis(raw) {
    try {
      var data = JSON.parse(String(raw || ""))
      root.emojiAll = Array.isArray(data) ? data : []
    } catch (ignore) {
      root.emojiAll = []
    }
  }

  function openEmoji() {
    // Leave the text field. While it has focus it eats Ctrl+E, and the
    // emoji list only receives keys when the catcher behind it is focused.
    root.emojiFilter = ""
    root.selected = 0
    root.screen = "emoji"
    root.composeFocus = false
    root.rebuildEmoji()
  }

  function rebuildEmoji() {
    emojiModel.clear()
    var needle = root.emojiFilter.toLowerCase()
    for (var i = 0; i < root.emojiAll.length; i++) {
      var item = root.emojiAll[i]
      if (!item || !item.e)
        continue
      var words = String(item.k || "").toLowerCase()
      if (needle && words.indexOf(needle) === -1)
        continue
      emojiModel.append({ emoji: String(item.e), keywords: String(item.k || "") })
      if (emojiModel.count >= 80)
        break
    }
    root.selected = 0
  }

  function insertEmoji() {
    if (root.selected < 0 || root.selected >= emojiModel.count)
      return
    var row = emojiModel.get(root.selected)
    var picture = row ? (row.emoji || "") : ""
    if (!picture)
      return
    composer.insert(composer.cursorPosition, picture)
    root.draft = composer.text
    root.screen = "inbox"
    root.composeFocus = true
  }

  function sendDraft() {
    if (!root.svc)
      return
    var body = root.draft
    if (!body.trim())
      return
    var thread = ""
    if (!root.toAddress && root.selected >= 0 && root.selected < shownModel.count) {
      var row = shownModel.get(root.selected)
      thread = row ? row.key : ""
    }
    root.svc.sendText(thread, root.toAddress, body)
    composer.clear()
    root.draft = ""
    root.toAddress = ""
    root.toLabel = ""
    root.composeFocus = false
  }

  function withEmojiFont(value) {
    // The card font has no emoji pictures. Qt on this desktop has no
    // "families" fallback, so emoji are wrapped in the emoji font.
    // The message is escaped first, so it cannot be read as markup.
    var raw = String(value || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    var out = ""
    var run = ""
    for (var i = 0; i < raw.length; i++) {
      var code = raw.charCodeAt(i)
      var emoji = (code >= 0xD800 && code <= 0xDFFF) || code === 0xFE0F || code === 0x200D || (code >= 0x2600 && code <= 0x27BF)
      if (emoji)
        run += raw.charAt(i)
      else {
        if (run) {
          out += "<span style=\"font-family: 'Noto Color Emoji'\">" + run + "</span>"
          run = ""
        }
        out += raw.charAt(i) === "\n" ? "<br/>" : raw.charAt(i)
      }
    }
    if (run)
      out += "<span style=\"font-family: 'Noto Color Emoji'\">" + run + "</span>"
    return out
  }

  function typingText(event) {
    // One emoji is several code units, so length is not always 1.
    // Control shortcuts, including paste, are left for the text field.
    if (!event.text)
      return false
    if (event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
      return false
    for (var i = 0; i < event.text.length; i++) {
      var code = event.text.charCodeAt(i)
      if (code < 32 || code === 127)
        return false
    }
    return true
  }

  function typeInto(target, character) {
    if (target === "filter")
      root.filterText += character
    else
      root.draft += character
  }

  function backspace(target) {
    if (target === "filter")
      root.filterText = root.filterText.slice(0, -1)
    else
      root.draft = root.draft.slice(0, -1)
  }

  function clockText(stamp) {
    // Stored times look like 2026-10-02T15:04:00. Today shows the clock.
    // Another day shows the date and the clock.
    var value = String(stamp || "")
    if (value.length < 16)
      return ""
    var hm = value.substring(11, 16)
    var now = new Date()
    var month = ("0" + (now.getMonth() + 1)).slice(-2)
    var day = ("0" + now.getDate()).slice(-2)
    var today = now.getFullYear() + "-" + month + "-" + day
    if (value.substring(0, 10) === today)
      return hm
    var names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    var monthName = names[parseInt(value.substring(5, 7), 10) - 1] || ""
    return parseInt(value.substring(8, 10), 10) + " " + monthName + "  " + hm
  }

  function stateLabel() {
    if (!root.svc)
      return "starting"
    var state = root.svc.textState
    if (state === "not_paired") return "not paired"
    if (state === "phone_nearby") return "phone nearby"
    if (state === "waiting_for_switches") return "waiting for iOS switches"
    if (state === "connected") return "connected"
    if (state === "pairing") return "pairing"
    if (state === "scanning") return "scanning"
    return state || "starting"
  }

  function hintLine() {
    if (root.helpOn)
      return "esc close help    j/k move    enter select    n new text    r reply    / filter    t notification text    y/n pairing    p explicit pair"
    if (root.screen === "code")
      return "y accept on this computer    n reject    esc cancel"
    if (root.screen === "checklist")
      return "t include message text in notifications (" + ((root.svc && root.svc.notifyBody) ? "on" : "off") + ")    esc back    ? help"
    if (root.screen === "contacts")
      return "type to filter    j/k move    enter choose    esc back    Sync Contacts on the iPhone fills this list    ? help"
    if (root.screen === "emoji")
      return "type to search    j/k move    enter insert    esc back    ? help"
    if (root.composeFocus) {
      var who = root.recipientLabel()
      return "enter send" + (who ? " to " + who : "") + "    ctrl+e emoji    esc back"
    }
    if (root.screen === "setup")
      return "j/k move    enter pair    enter scans again    p other pairing    esc close"
    return "j/k move    enter open    r reply    n new    / filter    esc close    ? help"
  }

  ListModel { id: shownModel }
  ListModel { id: emojiModel }
  property var emojiAll: []
  property string emojiFilter: ""

  FileView {
    // The desktop already ships a searchable emoji list. Read it, do not copy it.
    path: (root.omarchyPath || "/usr/share/omarchy") + "/shell/plugins/emojis/emojis.json"
    onLoaded: root.loadEmojis(text())
  }

  Connections {
    target: root.svc
    ignoreUnknownSignals: true
    function onTextStateChanged() { if (root.opened) root.followState(false) }
    function onPairCodeChanged() { if (root.opened && root.svc && root.svc.pairCode) root.screen = "code" }
    function onThreadRevisionChanged() { if (root.screen === "inbox") root.rebuildShown() }
    function onDeviceRevisionChanged() { if (root.screen === "setup") root.rebuildShown() }
    function onContactRevisionChanged() { if (root.screen === "contacts") root.selected = 0 }
  }

  onScreenChanged: root.rebuildShown()
  onFilterTextChanged: {
    if (root.screen === "contacts" && root.svc)
      root.svc.searchContacts(root.filterText)
    else
      root.rebuildShown()
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omaiphone"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: Math.min(root.cardWidth, panel.width - Style.gapsOut * 2)
      height: Math.min(root.cardHeight, panel.height - Style.gapsOut * 2)
      radius: root.cornerRadius > 0 ? root.cornerRadius : Style.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: !root.composeFocus

        Keys.priority: Keys.BeforeItem
        TextInput {
          id: composer
          // Invisible on purpose. Key presses land here so an emoji, a
          // composed character, or a paste becomes part of the reply.
          opacity: 0
          width: 1
          height: 1
          focus: root.composeFocus
          inputMethodHints: Qt.ImhNoAutoUppercase
          onTextChanged: root.draft = text
          // BeforeItem runs before the text field's own shortcuts. Ctrl+E
          // would otherwise jump to the end of the line and never open emojis.
          Keys.priority: Keys.BeforeItem
          Keys.onPressed: function(event) {
            var ctrl = (event.modifiers & Qt.ControlModifier) && !(event.modifiers & Qt.AltModifier)
            if (ctrl && event.key === Qt.Key_E) {
              root.openEmoji()
              event.accepted = true
              return
            }
            if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ControlModifier)) {
              root.sendDraft()
              event.accepted = true
              return
            }
            if (event.key === Qt.Key_Escape) {
              root.cancelCompose()
              event.accepted = true
            }
          }
        }

        Keys.onPressed: function(event) {
          var printable = root.typingText(event)

          if (event.key === Qt.Key_Question || (event.text === "?" && event.modifiers & Qt.ShiftModifier)) {
            root.helpOn = !root.helpOn
            event.accepted = true
            return
          }

          if (event.key === Qt.Key_Escape) {
            if (root.helpOn) root.helpOn = false
            else if (root.screen === "emoji") {
              root.screen = "inbox"
              root.composeFocus = true
            }
            else if (root.composeFocus) root.cancelCompose()
            else if (root.filterText) root.filterText = ""
            else if (root.screen === "contacts") root.screen = "inbox"
            else if (root.screen === "code") root.svc && root.svc.cancelPair()
            else if (root.screen === "checklist" && root.svc && root.svc.threads.count > 0) root.screen = "inbox"
            else root.dismiss()
            event.accepted = true
            return
          }

          if (root.screen === "code") {
            if (event.key === Qt.Key_Y && root.svc) root.svc.confirm(true)
            else if (event.key === Qt.Key_N && root.svc) root.svc.confirm(false)
            event.accepted = true
            return
          }

          if (root.screen === "checklist") {
            if (event.key === Qt.Key_T && root.svc) root.svc.setNotifyBody(!root.svc.notifyBody)
            event.accepted = true
            return
          }

          if (root.screen === "emoji") {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.insertEmoji()
            else if (event.key === Qt.Key_Backspace) {
              root.emojiFilter = root.emojiFilter.slice(0, -1)
              root.rebuildEmoji()
            } else if (event.key === Qt.Key_Down || (event.key === Qt.Key_J && !root.emojiFilter)) root.move(1)
            else if (event.key === Qt.Key_Up || (event.key === Qt.Key_K && !root.emojiFilter)) root.move(-1)
            else if (printable && event.text !== "/") {
              root.emojiFilter += event.text
              root.rebuildEmoji()
            }
            event.accepted = true
            return
          }

          if (root.composeFocus) {
            // Ctrl+E opens the emoji list. Enter sends. Other keys, including
            // paste, stay with the hidden text field.
            if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_E) root.openEmoji()
            else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.sendDraft()
            else {
              return
            }
            event.accepted = true
            return
          }

          if (root.screen === "contacts") {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.pickContact()
            else if (event.key === Qt.Key_Backspace) root.backspace("filter")
            else if (event.key === Qt.Key_Down || (event.key === Qt.Key_J && !root.filterText)) root.move(1)
            else if (event.key === Qt.Key_Up || (event.key === Qt.Key_K && !root.filterText)) root.move(-1)
            else if (printable && event.text !== "/") root.typeInto("filter", event.text)
            event.accepted = true
            return
          }

          // j and k move only when you are not in the middle of a filter.
          // Otherwise the letters j and k could never appear in a search.
          var filtering = root.filterText.length > 0
          if (event.key === Qt.Key_Down || (event.key === Qt.Key_J && !filtering)) root.move(1)
          else if (event.key === Qt.Key_Up || (event.key === Qt.Key_K && !filtering)) root.move(-1)
          else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (root.screen === "setup") {
              if (shownModel.count === 0 && root.svc) root.svc.scan()
              else root.pairSelected(false)
            } else root.composeFocus = true
          } else if (!filtering && event.key === Qt.Key_R && root.screen === "inbox") {
            root.toAddress = ""
            root.toLabel = ""
            root.startReply()
          } else if (!filtering && (event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_E && root.screen === "inbox") {
            root.startReply()
            root.openEmoji()
          } else if (!filtering && event.key === Qt.Key_N && root.screen === "inbox") {
            root.screen = "contacts"
            root.filterText = ""
            root.selected = 0
            root.composeFocus = false
            if (root.svc) root.svc.searchContacts("")
          } else if (!filtering && event.key === Qt.Key_P && root.screen === "setup") root.pairSelected(true)
          else if (event.key === Qt.Key_Slash) root.filterText = ""
          else if (event.key === Qt.Key_Backspace) root.backspace("filter")
          else if (printable) root.typeInto("filter", event.text)
          event.accepted = true
        }

        ColumnLayout {
          anchors.fill: parent
          anchors.topMargin: card.contentTopInset
          anchors.rightMargin: card.contentRightInset
          anchors.bottomMargin: card.contentBottomInset
          anchors.leftMargin: card.contentLeftInset
          spacing: Style.space(8)

          // The example card puts the name in the middle, then a quiet status.
          Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            textFormat: Text.PlainText
            text: "Messages"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            textFormat: Text.PlainText
            elide: Text.ElideRight
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: {
              if (root.screen === "code")
                return "Pairing code  " + (root.svc ? root.svc.pairCode : "")
              if (root.screen === "checklist")
                return "On the iPhone open Bluetooth, tap this computer, then turn on Show Message Notifications and Sync Contacts."
              if (root.screen === "emoji")
                return root.emojiFilter ? ("Emoji  " + root.emojiFilter) : "Pick an emoji"
              if (root.screen === "contacts")
                return "Choose who to text"
              if (root.screen === "setup")
                return root.svc && root.svc.textState === "scanning" ? "Looking for your iPhone" : "Select your iPhone"
              return root.stateLabel() + (root.svc && root.svc.phoneName ? "  " + root.svc.phoneName : "")
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            height: 1
            color: root.border
          }

          Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
              id: leftList
              width: root.screen === "inbox" ? parent.width * 0.38 : parent.width
              height: parent.height
              clip: true
              model: root.screen === "emoji" ? emojiModel : (root.screen === "contacts" && root.svc ? root.svc.contacts : shownModel)
              boundsBehavior: Flickable.StopAtBounds
              delegate: Rectangle {
                required property int index
                width: leftList.width
                height: Style.space(36)
                color: index === root.selected ? root.selectedBackground : "transparent"

                Text {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(8)
                  verticalAlignment: Text.AlignVCenter
                  textFormat: Text.RichText
                  elide: Text.ElideRight
                  text: root.withEmojiFont(root.rowText(index))
                  color: index === root.selected ? root.selectedText : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: root.screen === "inbox" && shownModel.get(index) !== undefined && (shownModel.get(index).unread || 0) > 0
                }

                MouseArea {
                  anchors.fill: parent
                  onClicked: {
                    root.selected = index
                    if (root.screen === "inbox") root.openSelectedThread()
                    else if (root.screen === "contacts") root.pickContact()
                    else if (root.screen === "setup") root.pairSelected(false)
                  }
                }
              }
            }

            Rectangle {
              visible: root.screen === "inbox"
              x: leftList.width
              width: 1
              height: parent.height
              color: root.border
            }

            ListView {
              id: transcript
              visible: root.screen === "inbox"
              x: leftList.width + Style.space(8)
              width: parent.width - leftList.width - Style.space(8)
              height: parent.height
              clip: true
              model: root.svc ? root.svc.messages : 0
              boundsBehavior: Flickable.StopAtBounds
              onCountChanged: if (count > 0) positionViewAtEnd()

              delegate: Item {
                required property string body
                required property string direction
                required property string timestamp
                width: transcript.width
                height: bubbleColumn.height + Style.space(10)

                // A hidden line measures the words so the bubble can hug them
                // instead of stretching across the whole conversation.
                Text {
                  id: measure
                  visible: false
                  // Plain text on purpose. Qt's default AutoText parser can
                  // treat a message as HTML and start an image download while
                  // it measures the width, even when this line is hidden.
                  textFormat: Text.PlainText
                  text: body
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }

                Column {
                  id: bubbleColumn
                  spacing: Style.space(3)
                  width: bubble.width
                  x: direction === "out" ? parent.width - width - Style.space(6) : Style.space(6)

                  Rectangle {
                    id: bubble
                    radius: Style.space(8)
                    color: direction === "out" ? root.selectedBackground : root.incomingBubble
                    width: bodyText.width + Style.space(20)
                    height: bodyText.implicitHeight + Style.space(14)

                    Text {
                      id: bodyText
                      x: Style.space(10)
                      y: Style.space(7)
                      width: Math.min(measure.implicitWidth, transcript.width * 0.62)
                      wrapMode: Text.Wrap
                      textFormat: Text.RichText
                      text: root.withEmojiFont(body)
                      color: direction === "out" ? root.selectedText : root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }
                  }

                  Text {
                    width: bubble.width
                    horizontalAlignment: direction === "out" ? Text.AlignRight : Text.AlignLeft
                    textFormat: Text.PlainText
                    text: (direction === "out" ? "sent" : "received") + "  " + root.clockText(timestamp)
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }

            Text {
              // No history with this person yet. Say their name here so the
              // blank side is not mistaken for someone else's conversation.
              visible: root.screen === "inbox" && transcript.count === 0 && leftList.count > 0
              x: leftList.width + Style.space(12)
              width: parent.width - leftList.width - Style.space(24)
              anchors.verticalCenter: parent.verticalCenter
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              text: root.recipientLabel()
                ? ("New text to " + root.recipientLabel())
                : "No texts in this conversation yet."
            }

            Text {
              anchors.centerIn: parent
              // The checklist has no rows. Still show the paired phone,
              // or the middle of the card looks like nothing was found.
              visible: root.screen === "checklist" || (leftList.count === 0 && root.screen !== "code")
              width: parent.width - Style.space(24)
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.7
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              text: {
                if (root.screen === "checklist") {
                  var phone = root.svc && root.svc.phoneName ? root.svc.phoneName : "iPhone"
                  var why = root.svc && root.svc.detail ? root.svc.detail : "Turn on Show Message Notifications."
                  return phone + " is paired.\n\n" + why
                }
                if (root.screen === "setup") {
                  if (root.svc && root.svc.textState === "scanning")
                    return "Looking for your iPhone…"
                  if (root.svc && root.svc.bluetoothReady)
                    return "No phone found. Press Enter to scan again."
                  return "Bluetooth is not ready. Press Enter to try again."
                }
                if (root.screen === "emoji")
                  return "No matching emoji."
                if (root.screen === "contacts")
                  return root.filterText
                    ? "No matching contacts."
                    : "No contacts yet.\n\nOn the iPhone open Bluetooth, tap this computer, and turn on Sync Contacts."
                return "No conversations yet.\n\nThe iPhone only shares texts that arrive after this connection. Older ones stay on the phone."
              }
            }
          }

          Rectangle {
            // The name you just chose, in the same solid bar as a selected row.
            Layout.fillWidth: true
            visible: root.composeFocus && root.recipientLabel().length > 0
            implicitHeight: toName.implicitHeight + Style.space(12)
            radius: Style.space(4)
            color: root.selectedBackground

            Text {
              id: toName
              anchors.fill: parent
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              verticalAlignment: Text.AlignVCenter
              textFormat: Text.PlainText
              elide: Text.ElideRight
              text: "To  " + root.recipientLabel()
              color: root.selectedText
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.composeFocus
            textFormat: Text.RichText
            elide: Text.ElideRight
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            text: root.withEmojiFont("> " + root.draft + (root.caretOn ? "▌" : " "))
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            height: 1
            color: root.border
          }

          // The example spreads the main keys across the bottom. Other
          // screens keep one hint line, because their keys change.
          RowLayout {
            Layout.fillWidth: true
            visible: root.screen === "inbox" && !root.composeFocus && !root.helpOn
            spacing: 0

            Text {
              text: "j/k move"
              Layout.fillWidth: true
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              text: "r reply"
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              text: "n new"
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              text: "ctrl+e emoji"
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              text: "esc close"
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignRight
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Text {
            Layout.fillWidth: true
            visible: !(root.screen === "inbox" && !root.composeFocus && !root.helpOn)
            textFormat: Text.PlainText
            elide: Text.ElideRight
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: root.hintLine()
          }
        }
      }
    }
  }
}
