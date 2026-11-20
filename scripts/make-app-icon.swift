// Renders bot-inator's robot app icon and packs it into AppIcon.icns.
//
//   swift scripts/make-app-icon.swift
//
// Writes MenuBarUI/MenuBarUI/Resources/AppIcon.icns (committed), so the
// build scripts only copy it. Re-run after changing the drawing below.
// Lives outside MenuBarUI/ because the app build compiles every .swift there.
import AppKit

let root = URL(fileURLWithPath: CommandLine.arguments[0])
    .deletingLastPathComponent().deletingLastPathComponent()
let outDir = root.appendingPathComponent("MenuBarUI/MenuBarUI/Resources")
let iconset = FileManager.default.temporaryDirectory
    .appendingPathComponent("AppIcon-\(getpid()).iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
try FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

func color(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

/// Draws in a 1024-unit square, y down.
func drawIcon(_ ctx: CGContext) {
    func rr(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ r: CGFloat) -> NSBezierPath {
        NSBezierPath(roundedRect: NSRect(x: x, y: y, width: w, height: h), xRadius: r, yRadius: r)
    }
    func oval(_ cx: CGFloat, _ cy: CGFloat, _ r: CGFloat) -> NSBezierPath {
        NSBezierPath(ovalIn: NSRect(x: cx - r, y: cy - r, width: 2 * r, height: 2 * r))
    }

    // Tile: macOS icon grid (824 pt body inside 1024), indigo gradient.
    let tile = rr(100, 100, 824, 824, 185)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = color(0x000000, 0.35)
    shadow.shadowBlurRadius = 24
    shadow.shadowOffset = NSSize(width: 0, height: -10)
    shadow.set()
    color(0x3B2F9E).setFill()
    tile.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(colors: [color(0x6C5CE7), color(0x3B2F9E)])!.draw(in: tile, angle: 90)

    // Antenna.
    color(0xD7DCE8).setFill()
    rr(494, 222, 36, 110, 18).fill()
    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = color(0xFFB347, 0.9)
    glow.shadowBlurRadius = 40
    glow.set()
    color(0xFFA62B).setFill()
    oval(512, 214, 46).fill()
    NSGraphicsContext.restoreGraphicsState()

    // Ears.
    color(0xB9C0D4).setFill()
    rr(196, 480, 70, 190, 30).fill()
    rr(758, 480, 70, 190, 30).fill()

    // Head.
    let head = rr(246, 320, 532, 470, 120)
    NSGradient(colors: [color(0xFFFFFF), color(0xDDE2EE)])!.draw(in: head, angle: 90)

    // Visor.
    let visor = rr(306, 410, 412, 230, 90)
    color(0x1E1B3A).setFill()
    visor.fill()

    // Eyes, glowing cyan.
    NSGraphicsContext.saveGraphicsState()
    let eyeGlow = NSShadow()
    eyeGlow.shadowColor = color(0x4BE3FF, 0.9)
    eyeGlow.shadowBlurRadius = 36
    eyeGlow.set()
    color(0x5CEBFF).setFill()
    oval(418, 520, 50).fill()
    oval(606, 520, 50).fill()
    NSGraphicsContext.restoreGraphicsState()
    color(0xFFFFFF, 0.85).setFill()
    oval(432, 504, 14).fill()
    oval(620, 504, 14).fill()

    // Smile.
    let smile = NSBezierPath()
    smile.move(to: NSPoint(x: 432, y: 694))
    smile.curve(to: NSPoint(x: 592, y: 694),
                controlPoint1: NSPoint(x: 470, y: 736), controlPoint2: NSPoint(x: 554, y: 736))
    smile.lineWidth = 26
    smile.lineCapStyle = .round
    color(0x3B2F9E).setStroke()
    smile.stroke()
}

func png(pixels: Int) throws -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let gc = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = gc
    let ctx = gc.cgContext
    let scale = CGFloat(pixels) / 1024
    ctx.translateBy(x: 0, y: CGFloat(pixels))
    ctx.scaleBy(x: scale, y: -scale)  // y down, 1024 units
    drawIcon(ctx)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try png(pixels: base).write(to: iconset.appendingPathComponent("icon_\(base)x\(base).png"))
    try png(pixels: base * 2).write(to: iconset.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
try png(pixels: 1024).write(to: outDir.appendingPathComponent("AppIcon-1024.png"))

let icns = outDir.appendingPathComponent("AppIcon.icns")
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", iconset.path, "-o", icns.path]
try p.run()
p.waitUntilExit()
try? FileManager.default.removeItem(at: iconset)
guard p.terminationStatus == 0 else { fatalError("iconutil failed") }
print("wrote \(icns.path)")
