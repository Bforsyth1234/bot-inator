import AppKit
import SwiftUI

/// bot-inator's robot face, drawn in code so the swiftc-only build needs no
/// asset catalog. A template image: AppKit and SwiftUI tint it (menu bar,
/// accent colour). The full-colour app icon comes from
/// scripts/make-app-icon.swift.
enum RobotGlyph {
    /// A template image `size` points square (the menu bar wants 18).
    static func image(size: CGFloat = 18) -> NSImage {
        let image = NSImage(size: NSSize(width: size, height: size), flipped: true) { rect in
            let s = rect.width / 18
            func r(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
                NSRect(x: x * s, y: y * s, width: w * s, height: h * s)
            }
            NSColor.black.setFill()
            NSColor.black.setStroke()

            // Antenna: stalk and bulb.
            let stalk = NSBezierPath()
            stalk.move(to: NSPoint(x: 9 * s, y: 2.6 * s))
            stalk.line(to: NSPoint(x: 9 * s, y: 5 * s))
            stalk.lineWidth = 1.3 * s
            stalk.stroke()
            NSBezierPath(ovalIn: r(7.6, 0.6, 2.8, 2.8)).fill()

            // Ears.
            NSBezierPath(roundedRect: r(0.6, 8, 1.8, 4.5), xRadius: 0.8 * s, yRadius: 0.8 * s).fill()
            NSBezierPath(roundedRect: r(15.6, 8, 1.8, 4.5), xRadius: 0.8 * s, yRadius: 0.8 * s).fill()

            // Head with eyes and mouth cut out (even-odd).
            let head = NSBezierPath(roundedRect: r(2.6, 4.8, 12.8, 11.4), xRadius: 3.2 * s, yRadius: 3.2 * s)
            head.append(NSBezierPath(ovalIn: r(5.0, 8.0, 3.0, 3.0)))
            head.append(NSBezierPath(ovalIn: r(10.0, 8.0, 3.0, 3.0)))
            head.append(NSBezierPath(roundedRect: r(6.2, 12.6, 5.6, 1.4), xRadius: 0.7 * s, yRadius: 0.7 * s))
            head.windingRule = .evenOdd
            head.fill()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "bot-inator"
        return image
    }
}

/// The robot as a SwiftUI view that takes `.foregroundStyle`.
struct RobotGlyphView: View {
    var size: CGFloat = 20
    var body: some View {
        Image(nsImage: RobotGlyph.image(size: size))
            .renderingMode(.template)
    }
}
