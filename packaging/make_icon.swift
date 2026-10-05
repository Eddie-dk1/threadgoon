import AppKit
import Foundation

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 0, bitsPerPixel: 0)!
        bitmap.size = NSSize(width: pixels, height: pixels)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let p = CGFloat(pixels)
        NSColor(calibratedRed: 0.17, green: 0.40, blue: 0.37, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: p * 0.06, y: p * 0.06, width: p * 0.88, height: p * 0.88),
                     xRadius: p * 0.20, yRadius: p * 0.20).fill()
        NSColor(calibratedWhite: 0.95, alpha: 0.95).setStroke()
        for offset in [0.0, 0.12, 0.24] {
            let rect = NSRect(x: p * 0.25, y: p * (0.25 + offset), width: p * 0.50, height: p * 0.22)
            let path = NSBezierPath(roundedRect: rect, xRadius: p * 0.035, yRadius: p * 0.035)
            path.lineWidth = p * 0.035
            path.stroke()
        }
        NSGraphicsContext.restoreGraphicsState()
        let data = bitmap.representation(using: .png, properties: [:])!
        let suffix = scale == 2 ? "@2x" : ""
        try data.write(to: directory.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
