import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import qs.Commons
import qs.Ui

// Bar icon plus the iPhone dropdown: calls, texts, the iPhone's notifications,
// music and settings. Everything comes from the shared service; every color
// and font comes from the bar and the Omarchy theme.
Panel {
  id: root
  moduleName: "io.github.dankestrick.omaiphone"
  // The service is shared by every monitor's bar; no per-instance IPC target.
  manageIpc: false

  readonly property var svc: bar && bar.shell ? bar.shell.serviceFor("io.github.dankestrick.omaiphone") : null
  readonly property var status: svc ? svc.status : null
  readonly property var phone: svc ? svc.phone : null
  readonly property var call: svc ? svc.call : null
  readonly property var caps: svc ? svc.caps : ({})
  readonly property var prefs: svc ? svc.settings : ({})
  readonly property bool connected: !!(svc && svc.connected)
  readonly property bool ringing: !!(svc && svc.ringing)
  readonly property bool inCall: !!(svc && svc.inCall)
  readonly property bool muted: !!(svc && svc.muted)
  readonly property string callState: svc ? svc.callState : "idle"
  readonly property int missed: svc ? svc.missedUnseen : 0
  readonly property int unread: svc ? svc.unread : 0
  readonly property int badge: missed + unread

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property color accent: Color.accent
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  function tint(c) { return Qt.rgba(c.r, c.g, c.b, 0.16) }

  readonly property var tabs: [
    { value: "Calls", label: "Calls", icon: "󰏲" },
    { value: "Texts", label: "Texts" + (unread > 0 ? " " + unread : ""), icon: "󰍦" },
    { value: "Notifications", label: "Alerts", icon: "󰂚", tooltip: "The iPhone's notifications" },
    { value: "Music", label: "Music", icon: "󰎆" },
    { value: "Settings", label: "", icon: "󰒓", tooltip: "Settings" }
  ]
  property string tab: "Calls"
  property string dialView: "keypad"      // keypad | contacts | recent
  property string dialNumber: ""
  property string contactQuery: ""
  property bool keypadOpen: false
  readonly property var filteredContacts: filterContacts(contactQuery)

  readonly property bool bluetoothOff: !!(status && status.bluetooth && !status.bluetooth.powered)

  readonly property string statusLine: {
    if (!status) return "Starting"
    if (bluetoothOff) return "Bluetooth is off"
    if (!phone) return "No iPhone paired"
    if (!connected) return phone.connected ? "Connecting" : "Not connected"
    if (ringing) return "Incoming call"
    if (callState === "dialing") return "Dialing"
    if (callState === "alerting") return "Ringing"
    if (callState === "active") return formatElapsed(svc.elapsed) + (caps.canTakeOver ? " · on the iPhone" : "")
    if (callState === "held") return "On hold"
    return "Connected" + (svc.battery !== null ? " · " + svc.battery + "%" : "")
  }

  // ---------------------------------------------------------------------------

  function run(args, stdin) { if (svc) svc.run(args, stdin) }

  function setTab(name) {
    tab = name
    if (name === "Calls" && dialView === "recent" && missed > 0) run(["history", "seen"])
  }

  function formatElapsed(seconds) {
    var h = Math.floor(seconds / 3600)
    var m = Math.floor((seconds % 3600) / 60)
    var s = seconds % 60
    function pad(v) { return v < 10 ? "0" + v : String(v) }
    return (h > 0 ? pad(h) + ":" : "") + pad(m) + ":" + pad(s)
  }

  function formatTime(iso) {
    var d = new Date(iso)
    if (isNaN(d.getTime())) return String(iso || "")
    var now = new Date()
    var time = Qt.formatTime(d, "h:mm ap")
    if (d.toDateString() === now.toDateString()) return time
    var yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
    if (d.toDateString() === yesterday.toDateString()) return "Yesterday"
    return Qt.formatDate(d, "MMM d")
  }

  function validNumber(value) {
    var cleaned = String(value || "").replace(/[\s\-.\/()]/g, "")
    return /^\+?[0-9*#]{2,21}$/.test(cleaned)
  }

  // Every word of the query in the name, or 3+ digits in the number.
  function filterContacts(query) {
    var entries = svc ? svc.contactEntries : []
    var q = String(query || "").toLowerCase().trim()
    var digits = q.replace(/[^0-9]/g, "")
    var words = q.split(/\s+/).filter(function(w) { return w !== "" })
    var result = []
    for (var i = 0; i < entries.length && result.length < 60; i++) {
      var entry = entries[i]
      var name = entry.name.toLowerCase()
      var ok = words.every(function(w) { return name.indexOf(w) >= 0 })
      if (!ok && digits.length >= 3) ok = entry.number.replace(/[^0-9]/g, "").indexOf(digits) >= 0
      if (ok) result.push(entry)
    }
    return result
  }

  function dial(number) {
    if (!caps.canDial || !validNumber(number)) return
    dialNumber = number
    // The number is stdin, not an argument. Other accounts can read argv.
    run(["call", "-"], String(number) + "\n")
  }

  function pressKey(key) {
    if (inCall && caps.canSendTones) run(["tones", "-"], String(key) + "\n")
    else dialNumber = dialNumber + key
  }

  function textPerson(number, name) {
    if (!svc) return
    root.close()
    svc.openMessages(number, name)
  }

  function openMessages() {
    if (!svc) return
    root.close()
    svc.openMessages("", "")
  }

  // Each monitor has its own bar; only the one on the focused monitor opens for a call.
  function onFocusedMonitor() {
    var window = button.QsWindow.window
    var focused = Hyprland.focusedMonitor
    if (!window || !window.screen || !focused) return true
    return window.screen.name === focused.name
  }

  Connections {
    target: root.svc
    function onRingRevisionChanged() {
      if (root.prefs.openOnIncomingCall === false || !root.onFocusedMonitor()) return
      root.tab = "Calls"
      root.open()
    }
    function onPanelRevisionChanged() {
      if (!root.onFocusedMonitor()) return
      if (root.svc.panelTab !== "") root.setTab(root.svc.panelTab)
      root.opened ? root.close() : root.open()
    }
    function onCallStateChanged() {
      if (root.svc.callState === "idle") root.keypadOpen = false
    }
  }

  onOpenedChanged: {
    if (!svc) return
    if (opened) {
      svc.refreshContacts()
      svc.refreshHistory()
      svc.loadThreads()
      if (tab === "Calls" && dialView === "recent" && missed > 0) run(["history", "seen"])
    } else {
      contactQuery = ""
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.inCall ? (root.muted ? "󰍭" : "󰏶") : "󰄜"
    dimmed: !root.connected
    tooltipText: root.opened ? "" : "iPhone · " + root.statusLine
      + (root.unread > 0 ? " · " + root.unread + " unread text" + (root.unread === 1 ? "" : "s") : "")
      + (root.missed > 0 ? " · " + root.missed + " missed call" + (root.missed === 1 ? "" : "s") : "")
    onPressed: function(b) {
      if (b === Qt.MiddleButton && root.caps.canHangup) root.run(["hangup"])
      else if (b === Qt.RightButton) root.openMessages()
      else root.toggle()
    }

    SequentialAnimation on opacity {
      running: root.ringing
      loops: Animation.Infinite
      onStopped: button.opacity = 1
      NumberAnimation { to: 0.35; duration: 450; easing.type: Easing.InOutQuad }
      NumberAnimation { to: 1; duration: 450; easing.type: Easing.InOutQuad }
    }

    // Missed calls + unread texts.
    Rectangle {
      visible: root.badge > 0 && !root.inCall
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.rightMargin: Style.space(1)
      anchors.topMargin: Style.space(2)
      width: Math.max(Style.space(12), badgeLabel.implicitWidth + Style.space(5))
      height: Style.space(12)
      radius: height / 2
      color: root.missed > 0 ? root.urgent : root.accent

      Text {
        id: badgeLabel
        anchors.centerIn: parent
        textFormat: Text.PlainText
        text: root.badge > 9 ? "9+" : String(root.badge)
        color: root.bar ? root.bar.background : Color.background
        font.family: root.fontFamily
        font.pixelSize: Style.space(8)
        font.bold: true
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: numberField.activeFocus || contactSearch.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        if (dx === 0 || root.ringing) return
        var names = root.tabs.map(function(t) { return t.value })
        var i = names.indexOf(root.tab) + dx
        root.setTab(names[(i + names.length) % names.length])
      }
      onActivateRequested: {
        if (root.caps.canAnswer) root.run(["answer"])
        else if (!root.inCall && root.tab === "Calls" && root.dialView === "keypad") root.dial(root.dialNumber)
      }
      onTextKey: function(key) {
        if (root.tab !== "Calls" || root.inCall && !root.keypadOpen) return
        if (root.dialView === "contacts" && !root.inCall) {
          contactSearch.forceActiveFocus()
          contactSearch.text = contactSearch.text + key
        } else if (/^[0-9*#+]$/.test(key)) {
          root.pressKey(key)
        }
      }

      // Faint OMAIPHONE behind the dropdown, out to its border.
      NameBackdrop {
        anchors.fill: column
        anchors.margins: -(panel.padding - Style.space(2))
        fontFamily: root.fontFamily
        color: root.accent
        copyWidth: 0.55
        strength: 0.12
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // ---------- Hero: phone · status ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "󰄜"
            color: root.ringing ? root.accent : root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            opacity: root.connected ? 1.0 : 0.5
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.phone && root.phone.name ? root.phone.name : "iPhone"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusLine.toUpperCase() + (root.inCall && root.muted ? " · MUTED" : "")
              color: root.inCall && root.muted ? root.urgent : (root.ringing ? root.accent : root.dim)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        // ---------- Not paired / not connected ----------
        Button {
          visible: root.status !== null && !root.phone
          width: parent.width
          iconText: "󰂱"
          text: "Pair iPhone"
          tooltipText: "Opens the Messages card, which walks you through pairing"
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.openMessages()
        }

        Button {
          visible: root.bluetoothOff
          width: parent.width
          iconText: "󰂯"
          text: "Turn on Bluetooth"
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          onClicked: {
            // The same switch as Omarchy's Bluetooth panel (an rfkill soft block).
            Quickshell.execDetached(["timeout", "10", "rfkill", "unblock", "bluetooth"])
            reconnectLater.restart()
          }
        }

        Timer {
          id: reconnectLater
          interval: 2500
          onTriggered: root.run(["connect"])
        }

        Button {
          visible: root.phone !== null && !root.connected && !root.bluetoothOff
          width: parent.width
          iconText: "󰂱"
          text: "Connect iPhone"
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.run(["connect"])
        }

        // ---------- A call: incoming, ringing out, or running ----------
        Column {
          visible: root.inCall
          width: parent.width
          spacing: Style.space(10)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.call ? (root.call.name || root.call.number || "Unknown caller") : ""
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            visible: !!(root.call && root.call.name && root.call.number)
            textFormat: Text.PlainText
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.call ? root.call.number : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Row {
            visible: root.ringing
            width: parent.width
            spacing: Style.space(8)

            Button {
              visible: root.caps.canReject === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰏵"
              text: "Decline"
              foreground: root.urgent
              background: root.tint(root.urgent)
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["reject"])
            }

            Button {
              visible: root.caps.canAnswer === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰏲"
              text: "Answer"
              foreground: root.accent
              background: root.tint(root.accent)
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["answer"])
            }
          }

          Row {
            visible: !root.ringing
            width: parent.width
            spacing: Style.space(8)

            Button {
              visible: root.caps.canMute === true
              width: (parent.width - parent.spacing) / 2
              iconText: root.muted ? "󰍭" : "󰍬"
              text: root.muted ? "Muted" : "Mute"
              tooltipText: root.muted ? "Your microphone is off. Click to turn it back on." : "Turn your microphone off"
              foreground: root.muted ? root.urgent : root.fg
              background: root.muted ? root.tint(root.urgent) : "transparent"
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["mute", root.muted ? "off" : "on"])
            }

            Button {
              visible: root.caps.canSendTones === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰌌"
              text: "Keypad"
              foreground: root.fg
              fontFamily: root.fontFamily
              bordered: true
              selected: root.keypadOpen
              onClicked: root.keypadOpen = !root.keypadOpen
            }
          }

          // Call volume as the iPhone reports it (0-15). At 0 the call is silent on the PC.
          Item {
            visible: !root.ringing && !!root.status && !!root.status.volume && root.status.volume.speaker !== null
                     && root.status.volume.speaker !== undefined
            width: parent.width
            implicitHeight: volumeSlider.implicitHeight

            Text {
              textFormat: Text.PlainText
              id: volumeIcon
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "󰕾"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
            }

            PanelSlider {
              id: volumeSlider
              bar: root.bar
              anchors.left: volumeIcon.right
              anchors.leftMargin: Style.space(10)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              minimum: 0
              maximum: 100
              step: 1
              integer: true
              value: root.status && root.status.volume ? Math.round(Number(root.status.volume.speaker || 0) * 100 / 15) : 0
              onReleased: function(v) { root.run(["volume", String(Math.round(v))]) }
            }
          }

          Button {
            visible: root.caps.canTakeOver === true
            width: parent.width
            iconText: "󰍹"
            text: "Take call on this PC"
            tooltipText: "The call is on the iPhone. Move it to this PC's microphone and speakers."
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.run(["take"])
          }

          Rectangle {
            visible: root.inCall && !root.ringing && root.muted
            width: parent.width
            implicitHeight: muteBanner.implicitHeight + Style.space(12)
            radius: Style.cornerRadius
            color: root.tint(root.urgent)
            border.color: root.urgent
            border.width: 1

            Text {
              id: muteBanner
              anchors.centerIn: parent
              width: parent.width - Style.space(16)
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              text: "󰍭  Microphone off. The caller can't hear you."
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
          }

          Grid {
            id: callKeypad
            visible: root.keypadOpen && root.caps.canSendTones === true
            columns: 3
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * 2) / 3

            Repeater {
              model: ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
              Button {
                required property string modelData
                width: callKeypad.cellWidth
                text: modelData
                fontSize: Style.font.title
                foreground: root.fg
                fontFamily: root.fontFamily
                bordered: true
                onClicked: root.pressKey(modelData)
              }
            }
          }

          Button {
            visible: !root.ringing && root.caps.canHangup === true
            width: parent.width
            iconText: "󰏵"
            text: "Hang up"
            foreground: root.urgent
            background: root.tint(root.urgent)
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.run(["hangup"])
          }
        }

        // ---------- Tabs ----------
        ButtonGroup {
          visible: !root.ringing
          options: root.tabs
          value: root.tab
          focusable: false
          foreground: root.fg
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onChanged: function(v) { root.setTab(v) }
        }

        PanelSeparator { visible: !root.ringing; foreground: root.fg }

        // ---------- Calls ----------
        Column {
          visible: root.tab === "Calls" && !root.inCall
          width: parent.width
          spacing: Style.space(10)

          ButtonGroup {
            options: [
              { value: "keypad", label: "Keypad", icon: "󰌌" },
              { value: "contacts", label: "Contacts", icon: "󰛋" },
              { value: "recent", label: "Recent" + (root.missed > 0 ? " · " + root.missed + " missed" : ""), icon: "󰋚" }
            ]
            value: root.dialView
            focusable: false
            foreground: root.fg
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onChanged: function(v) {
              root.dialView = v
              if (v === "contacts") contactSearch.forceActiveFocus()
              if (v === "recent" && root.missed > 0) root.run(["history", "seen"])
            }
          }

          // Keypad
          Column {
            visible: root.dialView === "keypad"
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: numberField
              width: parent.width
              foreground: root.fg
              placeholderText: "Phone number"
              text: root.dialNumber
              horizontalAlignment: TextInput.AlignHCenter
              font.pixelSize: Style.font.title
              onTextChanged: root.dialNumber = text
              onAccepted: root.dial(root.dialNumber)
              Keys.onEscapePressed: function(event) {
                if (text !== "") text = ""
                else root.close()
                event.accepted = true
              }
            }

            Grid {
              id: keypad
              columns: 3
              width: parent.width
              spacing: Style.space(6)
              readonly property real cellWidth: (width - spacing * 2) / 3

              Repeater {
                model: ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
                Button {
                  required property string modelData
                  width: keypad.cellWidth
                  text: modelData
                  fontSize: Style.font.title
                  foreground: root.fg
                  fontFamily: root.fontFamily
                  bordered: true
                  onClicked: root.pressKey(modelData)
                }
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(8)

              Button {
                width: parent.width - backspace.width - parent.spacing
                iconText: "󰏲"
                text: "Call"
                foreground: root.accent
                fontFamily: root.fontFamily
                bordered: true
                enabled: root.caps.canDial === true && root.validNumber(root.dialNumber)
                opacity: enabled ? 1 : 0.5
                onClicked: root.dial(root.dialNumber)
              }

              Button {
                id: backspace
                iconText: "󰭜"
                tooltipText: "Delete the last digit"
                foreground: root.fg
                fontFamily: root.fontFamily
                bordered: true
                enabled: root.dialNumber !== ""
                opacity: enabled ? 1 : 0.5
                onClicked: root.dialNumber = root.dialNumber.slice(0, -1)
              }
            }
          }

          // Contacts
          Column {
            visible: root.dialView === "contacts"
            width: parent.width
            spacing: Style.space(6)

            TextField {
              id: contactSearch
              width: parent.width
              foreground: root.fg
              placeholderText: root.svc && root.svc.contactEntries.length > 0
                ? "Search " + root.svc.contactEntries.length + " numbers" : "Search contacts"
              text: root.contactQuery
              onTextChanged: root.contactQuery = text
              onAccepted: if (root.filteredContacts.length > 0) root.dial(root.filteredContacts[0].number)
              Keys.onEscapePressed: function(event) {
                if (text !== "") text = ""
                else root.close()
                event.accepted = true
              }
            }

            Flickable {
              width: parent.width
              height: Math.min(contactColumn.implicitHeight, Style.space(300))
              contentHeight: contactColumn.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: contactColumn
                width: parent.width
                spacing: Style.space(2)

                Repeater {
                  model: root.filteredContacts
                  PersonRow {
                    required property var modelData
                    width: contactColumn.width
                    name: modelData.name
                    detail: modelData.number
                    number: modelData.number
                  }
                }
              }
            }

            Text {
              visible: root.filteredContacts.length === 0
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              textFormat: Text.PlainText
              text: root.svc && root.svc.contactEntries.length === 0
                ? "No contacts yet. On the iPhone, open Settings › Bluetooth, tap ⓘ next to this PC and turn on Sync Contacts."
                : "No contact found"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          // Recent calls
          Column {
            visible: root.dialView === "recent"
            width: parent.width
            spacing: Style.space(6)

            Flickable {
              width: parent.width
              height: Math.min(recentColumn.implicitHeight, Style.space(320))
              contentHeight: recentColumn.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: recentColumn
                width: parent.width
                spacing: Style.space(2)

                Repeater {
                  model: root.svc ? root.svc.historyEntries : []
                  PersonRow {
                    required property var modelData
                    readonly property bool wasMissed: modelData.type === "missed"
                    width: recentColumn.width
                    icon: wasMissed ? "󰏺" : (modelData.type === "dialed" ? "󰏻" : "󰏷")
                    alert: wasMissed
                    name: modelData.name || modelData.number || "Unknown"
                    detail: (wasMissed ? "Missed" : (modelData.type === "dialed" ? "Outgoing" : "Incoming"))
                      + " · " + root.formatTime(modelData.time)
                    number: modelData.number || ""
                  }
                }
              }
            }

            Text {
              visible: !root.svc || root.svc.historyEntries.length === 0
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              textFormat: Text.PlainText
              text: "No recent calls yet"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Button {
              width: parent.width
              iconText: "󰑐"
              text: "Get recent calls from the iPhone"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              onClicked: if (root.svc) root.svc.syncHistory()
            }
          }
        }

        // ---------- Texts ----------
        Column {
          visible: root.tab === "Texts" && !root.ringing
          width: parent.width
          spacing: Style.space(8)

          Text {
            visible: !!root.svc && root.svc.textState !== "connected"
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: {
              if (!root.svc) return ""
              if (root.phone && !root.connected) return "The iPhone is out of range or its Bluetooth is off. Texts come back when it reconnects."
              var detail = String(root.svc.detail || "")
              // obexd error names (br-connection-page-timeout and friends) mean nothing to people.
              if (detail === "" || /^[a-z]+(-[a-z]+)+$/.test(detail)) return "Connecting to the iPhone for texts…"
              return detail
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.svc ? root.svc.threads : null
            PersonRow {
              required property int index
              required property string label
              required property string preview
              required property int unread
              required property string timestamp
              required property string key
              visible: index < 6
              width: parent.width
              icon: unread > 0 ? "󰍡" : "󰍦"
              alert: false
              highlight: unread > 0
              name: label
              detail: preview
              trailing: root.formatTime(timestamp)
              onPicked: {
                root.close()
                root.svc.openMessages(key.indexOf(":") >= 0 ? key.slice(key.indexOf(":") + 1) : key, label)
              }
            }
          }

          Text {
            visible: !!root.svc && root.svc.threads.count === 0 && root.svc.textState === "connected"
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "No texts yet. New texts show up here. Older ones stay on the iPhone."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            width: parent.width
            iconText: "󰍩"
            text: "Open Messages"
            tooltipText: "Read and send texts in the Messages card (right-click the bar icon)"
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.openMessages()
          }
        }

        // ---------- The iPhone's notifications ----------
        Column {
          visible: root.tab === "Notifications" && !root.ringing
          width: parent.width
          spacing: Style.space(8)

          Toggle {
            width: parent.width
            label: "Show iPhone notifications"
            description: "Lists them here and pops them up on this PC. Texts pop up through Messages instead."
            checked: root.svc ? root.svc.mirrorOn : false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc && root.svc.setSetting("mirror", !root.svc.mirrorOn)
          }

          Text {
            visible: text !== ""
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: {
              var m = root.svc ? root.svc.mirror : null
              if (!m || !m.enabled) return ""
              if (m.state === "ready") return root.svc.inbox.length === 0
                ? "Nothing on the iPhone right now. If new ones never show up, open Settings › Bluetooth on the iPhone, tap ⓘ next to this PC and turn on Share System Notifications." : ""
              if (m.state === "needs-permission")
                return "The iPhone has not allowed it yet. Open Settings › Bluetooth on the iPhone, tap ⓘ next to this PC and turn on Share System Notifications."
              if (m.state === "unsupported") return "This phone offers no notification service."
              if (m.state === "waiting") return m.detail ? "Waiting: " + m.detail : "Waiting for the iPhone"
              return "Connecting to the iPhone…"
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          ListView {
            id: inboxList
            visible: !!root.svc && root.svc.mirrorOn && root.svc.inbox.length > 0
            width: parent.width
            height: Math.min(contentHeight, Style.space(320))
            clip: true
            spacing: Style.space(4)
            boundsBehavior: Flickable.StopAtBounds
            model: root.svc ? root.svc.inbox : []

            delegate: NotificationRow {
              required property var modelData
              width: inboxList.width
              entry: modelData
            }
          }

          Button {
            visible: !!root.svc && root.svc.mirrorOn && root.svc.inbox.length > 0
            width: parent.width
            iconText: "󰗩"
            text: "Clear all"
            tooltipText: "Clears them on the iPhone too"
            foreground: root.fg
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: root.run(["mirror", "dismiss", "all"])
          }
        }

        // ---------- Music on the iPhone ----------
        Column {
          visible: root.tab === "Music" && !root.ringing
          width: parent.width
          spacing: Style.space(10)

          Text {
            visible: !root.svc || !root.svc.hasMedia
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: root.svc && !root.svc.mirrorOn
              ? "Music controls share the link with iPhone notifications. Turn on Show iPhone notifications in Alerts to use them."
              : "Nothing is playing on the iPhone."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Column {
            visible: !!root.svc && root.svc.hasMedia
            width: parent.width
            spacing: Style.space(2)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              horizontalAlignment: Text.AlignHCenter
              text: root.svc && root.svc.media ? (root.svc.media.title || root.svc.media.player || "") : ""
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              horizontalAlignment: Text.AlignHCenter
              text: root.svc && root.svc.media
                ? [root.svc.media.artist, root.svc.media.album].filter(function(p) { return !!p }).join(" · ") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              horizontalAlignment: Text.AlignHCenter
              text: root.svc && root.svc.media && root.svc.media.player ? "Playing in " + root.svc.media.player : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            visible: !!root.svc && root.svc.hasMedia
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(10)

            Button {
              iconText: "󰒮"
              tooltipText: "Previous track"
              foreground: root.fg
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["media", "previous"])
            }

            Button {
              iconText: root.svc && root.svc.media && root.svc.media.playing ? "󰏤" : "󰐊"
              tooltipText: root.svc && root.svc.media && root.svc.media.playing ? "Pause" : "Play"
              foreground: root.fg
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["media", "toggle"])
            }

            Button {
              iconText: "󰒭"
              tooltipText: "Next track"
              foreground: root.fg
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["media", "next"])
            }
          }
        }

        // ---------- Settings ----------
        Column {
          visible: root.tab === "Settings" && !root.ringing
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader { text: "CALLS"; foreground: root.fg; fontFamily: root.fontFamily }

          Toggle {
            width: parent.width
            label: "Open this panel for incoming calls"
            checked: root.prefs.openOnIncomingCall !== false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc.setSetting("openOnIncomingCall", !checked)
          }

          Toggle {
            width: parent.width
            label: "Ring on this PC"
            checked: root.prefs.ringtone !== false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc.setSetting("ringtone", !checked)
          }

          Toggle {
            width: parent.width
            label: "Pop-up for incoming calls"
            description: "Click the pop-up to answer."
            checked: root.prefs.notifications !== false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc.setSetting("notifications", !checked)
          }

          Toggle {
            width: parent.width
            label: "Noise suppression"
            description: "Removes fan noise and echo from your microphone during calls."
            checked: root.prefs.noiseSuppression !== false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.run(["noise", checked ? "off" : "on"])
          }

          PanelSectionHeader { text: "TEXTS AND NOTIFICATIONS"; foreground: root.fg; fontFamily: root.fontFamily }

          Toggle {
            width: parent.width
            label: "Show the text in message pop-ups"
            description: "Off shows only who sent it."
            checked: !!root.svc && root.svc.notifyBody
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc.setNotifyBody(!checked)
          }

          Toggle {
            width: parent.width
            label: "Pop-ups for iPhone notifications"
            description: "Off keeps them in Alerts without popping up."
            checked: root.prefs.mirrorToasts !== false
            foreground: root.fg
            fontFamily: root.fontFamily
            onClicked: root.svc.setSetting("mirrorToasts", !checked)
          }

          PanelSectionHeader { text: "IPHONE"; foreground: root.fg; fontFamily: root.fontFamily }

          InfoRow { label: "Battery"; value: root.svc && root.svc.battery !== null ? root.svc.battery + "%" : "—" }
          InfoRow { label: "Contacts"; value: root.svc ? String(root.svc.contactEntries.length) + " numbers" : "—" }
          InfoRow {
            label: "Call audio"
            value: root.svc && root.svc.audioInfo
              ? root.shortDevice(root.svc.audioInfo.microphone) + " › " + root.shortDevice(root.svc.audioInfo.output) : "—"
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: (parent.width - parent.spacing) / 2
              iconText: "󰑐"
              text: "Sync contacts"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              enabled: root.connected
              opacity: enabled ? 1 : 0.5
              onClicked: {
                root.run(["contacts", "sync"])
                root.svc.refreshContacts()
              }
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              iconText: root.connected ? "󰂲" : "󰂱"
              text: root.connected ? "Disconnect" : "Connect"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              enabled: root.phone !== null
              opacity: enabled ? 1 : 0.5
              onClicked: root.run([root.connected ? "disconnect" : "connect"])
            }
          }
        }

        // ---------- Error from the last action ----------
        Text {
          visible: !!root.svc && root.svc.lastError !== ""
          width: parent.width
          wrapMode: Text.Wrap
          textFormat: Text.PlainText
          text: root.svc ? root.svc.lastError : ""
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }

  function shortDevice(name) {
    var value = String(name || "")
    if (value === "") return "—"
    if (value.indexOf("iphone_call") === 0) return "Noise suppressed"
    if (value.indexOf("bluez_") === 0) return "Bluetooth"
    if (value.indexOf("Razer") >= 0 || value.indexOf("Kraken") >= 0) return "Kraken headset"
    if (value.indexOf("usb") >= 0) return "USB audio"
    if (value.indexOf("hdmi") >= 0) return "HDMI"
    if (value.indexOf("analog") >= 0 || value.indexOf("pci") >= 0) return "Built-in audio"
    return value
  }

  // A person: icon, name, a second line, and Call / Text buttons.
  component PersonRow: CursorSurface {
    id: person
    property string icon: "󰀄"
    property string name: ""
    property string detail: ""
    property string number: ""
    property string trailing: ""
    property bool alert: false
    property bool highlight: false
    signal picked()
    foreground: root.fg
    hasCursor: rowMouse.containsMouse
    implicitHeight: personContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: {
        if (person.number !== "" && root.validNumber(person.number)) root.dial(person.number)
        person.picked()
      }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(4)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: person.icon
        color: person.alert ? root.urgent : (person.highlight ? root.accent : root.dim)
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: personContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: person.name
          color: person.alert ? root.urgent : root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: person.highlight
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          visible: text !== ""
          textFormat: Text.PlainText
          text: person.detail
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        visible: person.trailing !== ""
        textFormat: Text.PlainText
        text: person.trailing
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        Layout.alignment: Qt.AlignVCenter
      }

      PanelActionButton {
        visible: person.number !== ""
        iconText: "󰍦"
        tooltipText: "Text " + person.name
        foreground: root.fg
        fontFamily: root.fontFamily
        onClicked: root.textPerson(person.number, person.name)
      }

      PanelActionButton {
        visible: person.number !== "" && root.caps.canDial === true
        iconText: "󰏲"
        tooltipText: "Call " + person.name
        foreground: root.fg
        fontFamily: root.fontFamily
        onClicked: root.dial(person.number)
      }
    }
  }

  component NotificationRow: Rectangle {
    id: note
    property var entry: ({})
    height: noteColumn.implicitHeight + Style.space(12)
    radius: Math.min(Style.cornerRadius, Style.space(4))
    color: Util.alpha(root.fg, noteHover.hovered ? 0.09 : 0.05)

    HoverHandler { id: noteHover }

    Column {
      id: noteColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(6)
      anchors.rightMargin: note.entry.canDismiss ? dismissButton.width + Style.space(8) : Style.space(6)
      spacing: Style.space(1)

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: (note.entry.app || "iPhone") + "  ·  " + root.formatTime(note.entry.date)
        color: note.entry.important ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        visible: text !== ""
        textFormat: Text.PlainText
        text: [note.entry.title, note.entry.subtitle].filter(function(part) { return !!part }).join(" · ")
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        visible: text !== ""
        textFormat: Text.PlainText
        text: note.entry.message || ""
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.Wrap
        maximumLineCount: 3
        elide: Text.ElideRight
      }
    }

    PanelActionButton {
      id: dismissButton
      visible: note.entry.canDismiss === true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(2)
      anchors.verticalCenter: parent.verticalCenter
      iconText: "󰅖"
      tooltipText: "Clear on the iPhone"
      foreground: root.fg
      fontFamily: root.fontFamily
      onClicked: root.run(["mirror", "dismiss", String(note.entry.uid)])
    }
  }

  // Label on the left, value on the right.
  component InfoRow: Item {
    property string label: ""
    property string value: ""
    width: parent ? parent.width : 0
    implicitHeight: Math.max(labelText.implicitHeight, valueText.implicitHeight)

    Text {
      id: labelText
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: parent.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    Text {
      id: valueText
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: parent.value
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      font.bold: true
    }
  }
}
