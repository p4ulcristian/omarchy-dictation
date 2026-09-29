import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Dictation overlay for the Omarchy shell. iris-dictation broadcasts its state and the
// live voice level on $XDG_RUNTIME_DIR/iris-dictation/levels.sock (see PROTOCOL.md),
// one line per event:
//   recording | level 0.42 | transcribing | text <result> | typing <chars> | nothing | idle
// A "recording <tag>" belongs to the program that tagged it (Iris's desk card): the pill
// stays hidden until idle.
// One look from start to finish: a still dot on the left and three tapered neon
// sine waves. While recording the waves follow the voice. While waiting
// (transcribing, then typing the text out) they ease into a slow breath of their
// own; during typing they sit behind the text and light up from the left as it
// goes out.
Item {
  id: root

  property string mode: "hidden"      // hidden | recording | transcribing | result
  property string result: ""
  property real target: 0             // latest level from iris-dictation, 0..1
  property real amp: 0                // smoothed level the waves draw with
  property real phase: 0
  property real clock: 0              // seconds, drives the breath while waiting
  property real typed: 0              // 0..1, typing progress bar
  property bool typing: false
  property bool quiet: false          // a tagged recording ("recording iris"): its owner shows it
  // Waiting on iris-dictation (transcribing, then typing a long result): the waves
  // breathe on their own instead of following the voice.
  readonly property bool loading: mode === "transcribing" || (mode === "result" && typing)

  readonly property bool shown: mode !== "hidden"
  // Cyberpunk neon: cyan, hot magenta, electric yellow.
  readonly property var hues: ["#05d9e8", "#ff2a6d", "#fcee0a"]
  readonly property int pillW: 460
  readonly property int pillH: 88

  readonly property var targetScreen: {
    var name = Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : ""
    var screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++)
      if (screens[i].name === name) return screens[i]
    return screens.length > 0 ? screens[0] : null
  }

  function handle(line) {
    var space = line.indexOf(" ")
    var verb = space < 0 ? line : line.slice(0, space)
    var arg = space < 0 ? "" : line.slice(space + 1)
    // A recording another program asked for with a tag, and shows itself: stay out
    // of it until the daemon is idle again.
    if (verb === "recording" && arg !== "") {
      root.quiet = true
      hideTimer.stop()
      root.mode = "hidden"
      return
    }
    if (root.quiet) {
      if (verb === "idle") root.quiet = false
      return
    }
    if (verb === "level") {
      root.target = parseFloat(arg) || 0
    } else if (verb === "recording") {
      hideTimer.stop()
      root.result = ""
      root.target = 0
      root.mode = "recording"
    } else if (verb === "transcribing") {
      root.target = 0
      root.mode = "transcribing"
    } else if (verb === "text") {
      // Stays up until iris-dictation says idle, however long the typing takes.
      root.result = arg
      root.typing = false
      typedAnim.stop()
      root.typed = 0
      root.mode = "result"
    } else if (verb === "typing") {
      // wtype manages about 220 chars/s; fill the bar at that pace.
      var ms = (parseInt(arg) || 0) / 220 * 1000
      root.typing = ms > 150
      if (root.typing) {
        typedAnim.duration = ms
        typedAnim.restart()
      }
    } else if (verb === "nothing") {
      root.result = ""
      root.typing = false
      root.mode = "result"
    } else if (verb === "idle") {
      // A result lingers a moment after typing finishes; anything else hides now.
      if (root.mode === "result") {
        typedAnim.stop()
        root.typed = 1
        hideTimer.interval = root.result !== "" ? 1200 : 900
        hideTimer.restart()
      } else {
        root.mode = "hidden"
      }
    }
  }

  NumberAnimation {
    id: typedAnim
    target: root
    property: "typed"
    from: 0
    to: 0.97                          // the last bit is left for "idle"
  }

  Timer {
    id: hideTimer
    onTriggered: root.mode = "hidden"
  }

  // The connection to iris-dictation. It restarts, or starts after the shell, and
  // its socket only appears once the model has loaded, so keep knocking until it
  // answers. After a refused attempt a Socket will not try again, so each retry
  // builds a fresh one.
  Loader {
    id: link
    sourceComponent: Socket {
      path: Quickshell.env("XDG_RUNTIME_DIR") + "/iris-dictation/levels.sock"
      connected: true
      parser: SplitParser { onRead: data => root.handle(data) }
      onConnectedChanged: if (!connected) {
        root.mode = "hidden"          // don't leave a frozen pill up
        retry.restart()
      }
      onError: retry.restart()
    }
  }

  Timer {
    id: retry
    interval: 2000
    onTriggered: {
      link.active = false
      link.active = true
    }
  }

  // Runs while the pill is up. amp always eases toward its goal, so the voice
  // hands over to the breath (and the breath to rest) without a jump.
  FrameAnimation {
    running: root.shown
    onTriggered: {
      root.clock += frameTime
      var goal = root.mode === "recording" ? root.target
               : root.loading ? 0.3 + 0.12 * Math.sin(root.clock * 2)
               : 0
      // Rise fast with the voice, settle a little slower.
      var k = goal > root.amp ? 0.35 : 0.12
      root.amp += (goal - root.amp) * k
      root.phase += frameTime * (5.5 + 5 * root.amp)
      if (wave.visible) wave.requestPaint()
    }
  }

  PanelWindow {
    visible: root.shown
    screen: root.targetScreen
    // Top right, where notifications appear, clear of the text being written.
    anchors { top: true; right: true }
    implicitWidth: root.pillW + 12
    implicitHeight: root.pillH + 12
    color: "transparent"
    WlrLayershell.namespace: "iris-dictation"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    // Sit below the bar rather than over it.
    exclusionMode: ExclusionMode.Normal
    // Look-only: never takes a click.
    mask: Region {}

    BorderSurface {
      id: pill
      width: root.pillW
      height: root.pillH
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.rightMargin: 12
      anchors.topMargin: 12
      radius: Style.cornerRadius
      color: Util.alpha(Color.background, 0.9)
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))

      // The dot: a fixed marker for as long as the pill is up.
      Rectangle {
        x: 22
        anchors.verticalCenter: parent.verticalCenter
        width: 14
        height: width
        radius: width / 2
        gradient: Gradient {
          GradientStop { position: 0; color: root.hues[0] }
          GradientStop { position: 1; color: root.hues[1] }
        }
      }

      Canvas {
        id: wave
        visible: root.mode === "recording" || root.loading
        anchors.fill: parent
        anchors.leftMargin: 48
        anchors.rightMargin: 24
        antialiasing: true

        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          var w = width, h = height, mid = h / 2
          var maxA = h * 0.42
          // Three neon sine waves: different speeds and wavelengths, the front
          // one strongest, each with a glow. Overlaps add up brighter.
          ctx.globalCompositeOperation = "lighter"
          var layers = [
            { freq: 2.2, speed: 1.0,  alpha: 1.0,  width: 2.5, scale: 1.0 },
            { freq: 3.1, speed: -0.7, alpha: 0.8,  width: 2,   scale: 0.7 },
            { freq: 1.6, speed: 0.45, alpha: 0.6,  width: 1.5, scale: 0.5 }
          ]
          // While typing: dim behind the text, lit up to how far typing got.
          var passes = root.typing
              ? [{ x1: w, alpha: 0.22 }, { x1: w * root.typed, alpha: 0.75 }]
              : [{ x1: w, alpha: 1.0 }]
          for (var p = 0; p < passes.length; p++) {
            ctx.save()
            ctx.beginPath()
            ctx.rect(0, 0, passes[p].x1, h)
            ctx.clip()
            for (var l = 0; l < layers.length; l++) {
              var L = layers[l]
              ctx.beginPath()
              ctx.lineWidth = L.width
              ctx.strokeStyle = Util.alpha(root.hues[l], L.alpha * passes[p].alpha)
              ctx.shadowColor = root.hues[l]
              ctx.shadowBlur = passes[p].alpha > 0.5 ? 8 : 0
              for (var x = 0; x <= w; x += 3) {
                var t = x / w
                // Taper to zero at both ends so the wave floats in the pill.
                var env = Math.pow(Math.sin(Math.PI * t), 2)
                var y = mid + Math.sin(t * Math.PI * 2 * L.freq + root.phase * L.speed)
                          * maxA * L.scale * (0.04 + root.amp) * env
                if (x === 0) ctx.moveTo(x, y)
                else ctx.lineTo(x, y)
              }
              ctx.stroke()
            }
            ctx.restore()
          }
        }
      }

      // After transcription: what was heard (or that nothing was).
      Text {
        visible: root.mode === "result"
        anchors.fill: parent
        anchors.leftMargin: 48              // clear of the dot
        anchors.rightMargin: 24
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        textFormat: Text.PlainText
        text: root.result !== "" ? root.result : "nothing heard"
        elide: Text.ElideRight
        maximumLineCount: 2
        wrapMode: Text.WordWrap
        font.family: Style.font.family
        font.pixelSize: Style.font.heading
        style: root.typing ? Text.Outline : Text.Normal
        styleColor: Util.alpha(Color.background, 0.8)
        color: root.result !== "" ? Color.popups.text : Util.alpha(Color.popups.text, 0.55)
      }
    }
  }
}
