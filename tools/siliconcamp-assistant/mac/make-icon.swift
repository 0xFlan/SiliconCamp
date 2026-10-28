import AppKit

let pixels = 1024
let size = CGFloat(pixels)

guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: pixels,
    pixelsHigh: pixels,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
    fatalError("cannot create the icon bitmap")
}

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(calibratedRed: red, green: green, blue: blue, alpha: alpha)
}

func diamond(center: NSPoint, side: CGFloat, colors: [NSColor], stroke: NSColor, lineWidth: CGFloat, shadow: Bool) {
    NSGraphicsContext.saveGraphicsState()
    let transform = NSAffineTransform()
    transform.translateX(by: center.x, yBy: center.y)
    transform.rotate(byDegrees: 45)
    transform.concat()

    let rect = NSRect(x: -side / 2, y: -side / 2, width: side, height: side)
    let path = NSBezierPath(roundedRect: rect, xRadius: side * 0.19, yRadius: side * 0.19)

    if shadow {
        let shadow = NSShadow()
        shadow.shadowBlurRadius = side * 0.16
        shadow.shadowOffset = NSSize(width: 0, height: -side * 0.06)
        shadow.shadowColor = color(0, 0.08, 0.22, 0.5)
        shadow.set()
    }

    NSGradient(colors: colors)?.draw(in: path, angle: -50)
    stroke.setStroke()
    path.lineWidth = lineWidth
    path.stroke()

    NSGraphicsContext.restoreGraphicsState()
}

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
context.cgContext.clear(CGRect(x: 0, y: 0, width: size, height: size))

// The macOS icon grid: a squircle inset from a transparent 1024 canvas.
let inset: CGFloat = 82
let body = NSRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
let squircle = NSBezierPath(roundedRect: body, xRadius: 214, yRadius: 214)
squircle.addClip()

NSGradient(colors: [
    color(0.055, 0.115, 0.23),
    color(0.035, 0.075, 0.16),
    color(0.015, 0.035, 0.085),
])?.draw(in: body, angle: -90)

// A soft bloom behind the glass, clipped to the icon body.
let bloom = NSBezierPath(ovalIn: NSRect(x: 185, y: 300, width: 660, height: 560))
color(0.08, 0.38, 0.9, 0.34).setFill()
bloom.fill()

let bloomCore = NSBezierPath(ovalIn: NSRect(x: 335, y: 420, width: 360, height: 340))
color(0.24, 0.6, 1, 0.22).setFill()
bloomCore.fill()

// Specular sheen across the upper-left corner.
let sheen = NSBezierPath()
sheen.move(to: NSPoint(x: 120, y: 820))
sheen.line(to: NSPoint(x: 540, y: 960))
sheen.line(to: NSPoint(x: 300, y: 650))
sheen.close()
color(1, 1, 1, 0.055).setFill()
sheen.fill()

// The two rear glass facets.
diamond(
    center: NSPoint(x: 386, y: 512),
    side: 300,
    colors: [
        color(0.95, 0.98, 1, 0.9),
        color(0.62, 0.72, 0.87, 0.42),
        color(0.83, 0.9, 1, 0.62),
    ],
    stroke: color(1, 1, 1, 0.62),
    lineWidth: 3,
    shadow: true
)
diamond(
    center: NSPoint(x: 638, y: 512),
    side: 300,
    colors: [
        color(0.84, 0.91, 1, 0.72),
        color(0.47, 0.59, 0.77, 0.34),
        color(0.92, 0.96, 1, 0.5),
    ],
    stroke: color(1, 1, 1, 0.48),
    lineWidth: 3,
    shadow: true
)

// The blue center facet.
diamond(
    center: NSPoint(x: 512, y: 512),
    side: 354,
    colors: [
        color(0.62, 0.84, 1),
        color(0.1, 0.52, 0.98),
        color(0.0, 0.31, 0.72),
    ],
    stroke: color(0.85, 0.95, 1, 0.8),
    lineWidth: 4,
    shadow: true
)

// A restrained inner rim, like the edge of a glass window.
let rim = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 212, yRadius: 212)
color(1, 1, 1, 0.16).setStroke()
rim.lineWidth = 3
rim.stroke()

NSGraphicsContext.restoreGraphicsState()

guard let data = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("cannot encode the icon")
}
let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png")
try data.write(to: output)
print(output.path)
