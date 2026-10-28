import AppKit
import Darwin
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private let port = 8770
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var ownsServer = false
    private var logHandle: FileHandle?
    private var screenshotPath: String?
    private var didCaptureScreenshot = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        parseScreenshotArgument()
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        buildWindow()

        guard let root = repositoryRoot() else {
            showFailure(
                title: "Cannot find the SiliconCamp checkout",
                detail: "Build and open this app from inside the repository. It reads the checkout rather than downloading anything."
            )
            return
        }

        if serverIsRunningWithRetries() {
            loadWalkthrough()
        } else if portIsOpen() {
            showFailure(
                title: "Port 8770 is already in use",
                detail: "Another process is listening there. Quit it and reopen SiliconCamp Assistant, or stop the other assistant server."
            )
        } else {
            startServer(root: root)
        }

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if ownsServer, let server, server.isRunning {
            server.terminate()
        }
        try? logHandle?.close()
    }

    // MARK: - Window

    private func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1080, height: 720)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "SiliconCamp Assistant"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = .clear
        window.isOpaque = false
        window.minSize = NSSize(width: 900, height: 620)
        window.center()
        window.setFrameAutosaveName("SiliconCampAssistantWindow")

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let web = WKWebView(frame: frame, configuration: configuration)
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.isInspectable = true
        window.contentView = web
        webView = web
    }

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About SiliconCamp Assistant",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit SiliconCamp Assistant",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewMenu.addItem(.separator())
        viewMenu.addItem(
            withTitle: "Enter Full Screen",
            action: #selector(NSWindow.toggleFullScreen(_:)),
            keyEquivalent: "f"
        )
        viewItem.submenu = viewMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func reload() {
        loadWalkthrough()
    }

    // MARK: - Server

    private func repositoryRoot() -> URL? {
        if let explicit = ProcessInfo.processInfo.environment["SILICONCAMP_ROOT"],
           !explicit.isEmpty {
            return URL(fileURLWithPath: explicit)
        }

        var candidate = Bundle.main.bundleURL
        for _ in 0..<10 {
            let server = candidate.appendingPathComponent("tools/siliconcamp-assistant/server.py")
            if FileManager.default.fileExists(atPath: server.path) {
                return candidate
            }
            candidate.deleteLastPathComponent()
        }

        let current = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let server = current.appendingPathComponent("tools/siliconcamp-assistant/server.py")
        if FileManager.default.fileExists(atPath: server.path) {
            return current
        }
        return nil
    }

    private func statusURL() -> URL? {
        URL(string: "http://127.0.0.1:\(port)/api/status")
    }

    private func portIsOpen() -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(port).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")

        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }

    private func serverIsRunningWithRetries() -> Bool {
        for attempt in 0..<3 {
            if serverIsRunning() {
                return true
            }
            if attempt < 2 {
                usleep(80_000)
            }
        }
        return false
    }

    private func serverIsRunning() -> Bool {
        guard let url = statusURL() else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.35
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let semaphore = DispatchSemaphore(value: 0)
        var reachable = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                reachable = true
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 0.5)
        return reachable
    }

    private func startServer(root: URL) {
        let serverScript = root.appendingPathComponent("tools/siliconcamp-assistant/server.py")
        let logDirectory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        let logURL = logDirectory.appendingPathComponent("SiliconCampAssistant.log")
        try? FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [serverScript.path, "--port", "\(port)"]
        process.currentDirectoryURL = root

        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.truncateFile(atOffset: 0)
            process.standardOutput = handle
            process.standardError = handle
            logHandle = handle
        }

        do {
            try process.run()
            server = process
            ownsServer = true
            waitForServer(attempts: 80)
        } catch {
            showFailure(
                title: "Cannot start the local assistant",
                detail: "python3 could not run tools/siliconcamp-assistant/server.py: \(error.localizedDescription)"
            )
        }
    }

    private func waitForServer(attempts: Int) {
        if serverIsRunning() {
            loadWalkthrough()
            return
        }
        guard attempts > 0 else {
            showFailure(
                title: "The local assistant did not start",
                detail: "See ~/Library/Logs/SiliconCampAssistant.log for the server output."
            )
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            self.waitForServer(attempts: attempts - 1)
        }
    }

    // MARK: - Content

    private func loadWalkthrough() {
        guard let url = URL(string: "http://127.0.0.1:\(port)/?native=1") else { return }
        webView.load(URLRequest(url: url))
    }

    private func parseScreenshotArgument() {
        let arguments = CommandLine.arguments
        guard let index = arguments.firstIndex(of: "--screenshot"),
              arguments.indices.contains(index + 1) else { return }
        screenshotPath = arguments[index + 1]
    }

    private func captureScreenshotIfRequested() {
        guard let path = screenshotPath, !didCaptureScreenshot else { return }
        guard let currentURL = webView.url?.absoluteString,
              currentURL.contains("?native=1") else { return }
        didCaptureScreenshot = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            fputs("capturing \(self.webView.url?.absoluteString ?? "no url")\n", stderr)
            self.webView.takeSnapshot(with: nil) { image, error in
                guard let image,
                      let tiff = image.tiffRepresentation,
                      let bitmap = NSBitmapImageRep(data: tiff),
                      let png = bitmap.representation(using: .png, properties: [:]) else {
                    fputs("screenshot failed: \(error?.localizedDescription ?? "no image")\n", stderr)
                    NSApp.terminate(nil)
                    return
                }
                do {
                    try png.write(to: URL(fileURLWithPath: path))
                    print(path)
                } catch {
                    fputs("cannot write screenshot: \(error.localizedDescription)\n", stderr)
                }
                NSApp.terminate(nil)
            }
        }
    }

    private func showFailure(title: String, detail: String) {
        let html = """
        <!doctype html>
        <html lang="en">
        <meta charset="utf-8">
        <style>
          :root { color-scheme: light dark; }
          body {
            margin: 0; height: 100vh; display: grid; place-items: center;
            background: transparent; color: color-mix(in srgb, CanvasText 88%, transparent);
            font: 13px -apple-system, BlinkMacSystemFont, sans-serif;
          }
          main { max-width: 430px; padding: 28px; text-align: center; }
          h1 { margin: 0 0 8px; font-size: 17px; }
          p { margin: 0; color: color-mix(in srgb, CanvasText 58%, transparent); line-height: 1.5; }
        </style>
        <main>
          <h1>\(title)</h1>
          <p>\(detail)</p>
        </main>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        captureScreenshotIfRequested()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showFailure(title: "The assistant window could not load", detail: error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showFailure(title: "The assistant window could not load", detail: error.localizedDescription)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
