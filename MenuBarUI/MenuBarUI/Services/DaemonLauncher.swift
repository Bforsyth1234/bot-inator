import AppKit
import Darwin
import Foundation

/// Starts and stops the daemon bundled inside the release app (#96).
///
/// The release build (`scripts/release/build-release.sh`) puts a relocatable
/// Python in `Contents/Resources/python` and the daemon's sources in
/// `Contents/Resources/agent-daemon`. When those are there AND nothing
/// answers `http://127.0.0.1:8000/health`, the app starts the daemon itself,
/// so macOS permissions belong to the app, not a terminal. A dev build
/// (`./install.sh`) has neither folder, and a daemon from `./run.sh` already
/// answers, so in both cases this does nothing.
///
/// The daemon it started is stopped when the app quits, restarted with
/// backoff (1, 2, 4, 8, 16 s) if it exits, and after 5 quick failures in a
/// row the app says so with the end of the log. Its home for files that
/// used to live in its own tree (`.env`, generated tools) is
/// `~/Library/Application Support/bot-inator` (`AGENT_DATA_DIR`); its log is
/// `~/Library/Logs/bot-inator/daemon.log`.
@MainActor
final class DaemonLauncher: ObservableObject {
    static let shared = DaemonLauncher()

    enum State: Equatable {
        /// Dev build: no bundled daemon (`./run.sh` starts one).
        case notBundled
        /// Looking for a running daemon / starting ours.
        case checking
        /// Another daemon (e.g. `./run.sh`) answers on the port; left alone.
        case external
        case running(pid: Int32)
        case restarting(attempt: Int, delay: Int)
        case failed(String)
        case stopped
    }

    @Published private(set) var state: State = .notBundled

    /// 8000, the port the app's clients use. `BOT_INATOR_PORT` (as in
    /// `run.sh`) moves it for testing the launcher only.
    nonisolated static let port: Int =
        Int(ProcessInfo.processInfo.environment["BOT_INATOR_PORT"] ?? "") ?? 8000
    nonisolated static let healthURL = URL(string: "http://127.0.0.1:\(port)/health")!
    static let maxQuickFailures = 5
    /// A run this long counts as healthy: the next exit starts the backoff over.
    static let healthyUptime: TimeInterval = 120

    struct Layout: Equatable {
        let python: URL
        let daemonDir: URL
    }

    /// The bundled Python and daemon, or nil in a dev build.
    static func bundledLayout(resources: URL? = Bundle.main.resourceURL) -> Layout? {
        guard let resources else { return nil }
        let python = resources.appendingPathComponent("python/bin/python3")
        let daemon = resources.appendingPathComponent("agent-daemon")
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: python.path),
              fm.fileExists(atPath: daemon.appendingPathComponent("main.py").path) else {
            return nil
        }
        return Layout(python: python, daemonDir: daemon)
    }

    /// Same command line as `scripts/release/smoke-test.sh`. `-I`: no user
    /// site-packages, no PYTHONPATH; `-B`: never write .pyc into the signed
    /// bundle (they are compiled at build time).
    static func arguments(for layout: Layout, port: Int = port) -> [String] {
        ["-I", "-B", "-m", "uvicorn", "main:app", "--app-dir", layout.daemonDir.path,
         "--host", "127.0.0.1", "--port", String(port)]
    }

    static func environment(base: [String: String], dataDir: URL) -> [String: String] {
        var env = base
        env["AGENT_DATA_DIR"] = dataDir.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for key in ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"] {
            env.removeValue(forKey: key)
        }
        return env
    }

    /// Restart delay after `failures` quick exits in a row (1-based).
    static func backoff(afterFailures failures: Int) -> Int {
        min(60, 1 << max(0, min(failures - 1, 6)))
    }

    static var dataDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("bot-inator", isDirectory: true)
    }

    static var logURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/bot-inator/daemon.log")
    }

    private static var pidFile: URL { dataDir.appendingPathComponent("daemon.pid") }

    private var layout: Layout?
    private var process: Process?
    /// A daemon of ours left running by an earlier app run that did not
    /// quit cleanly (force quit, crash): found by its pid file.
    private var adoptedPID: pid_t?
    private var stopping = false
    private var quickFailures = 0
    private var startedAt = Date.distantPast
    private var restartTask: Task<Void, Never>?

    private init() {}

    var isBundled: Bool { Self.bundledLayout() != nil }

    // MARK: start / stop

    /// Called once from `applicationDidFinishLaunching`.
    func start() {
        guard let layout = Self.bundledLayout() else {
            state = .notBundled
            return
        }
        self.layout = layout
        stopping = false
        state = .checking
        Task { await self.startIfNoDaemon() }
    }

    /// "Try again" after a failure.
    func retry() {
        quickFailures = 0
        restartTask?.cancel()
        start()
    }

    /// Called from `applicationWillTerminate`: SIGTERM (uvicorn shuts down
    /// cleanly), then SIGKILL after 5 s.
    func stop() {
        stopping = true
        restartTask?.cancel()
        if let process, process.isRunning {
            process.terminate()
            Self.waitForExit(pid: process.processIdentifier, seconds: 5)
        } else if let pid = adoptedPID {
            kill(pid, SIGTERM)
            Self.waitForExit(pid: pid, seconds: 5)
        }
        try? FileManager.default.removeItem(at: Self.pidFile)
        process = nil
        adoptedPID = nil
        state = .stopped
    }

    func showLog() {
        NSWorkspace.shared.activateFileViewerSelecting([Self.logURL])
    }

    private func startIfNoDaemon() async {
        guard !stopping, let layout else { return }
        if await Self.healthAnswers() {
            if let pid = Self.ourOrphan(python: layout.python) {
                adoptedPID = pid
                state = .running(pid: pid)
            } else {
                state = .external
            }
            return
        }
        launch(layout)
    }

    private func launch(_ layout: Layout) {
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: Self.dataDir, withIntermediateDirectories: true)
            try fm.createDirectory(at: Self.logURL.deletingLastPathComponent(),
                                   withIntermediateDirectories: true)
        } catch {
            fail("Couldn't create \(Self.dataDir.path): \(error.localizedDescription)")
            return
        }
        let log: FileHandle
        do {
            log = try Self.openLog()
        } catch {
            fail("Couldn't open the daemon log: \(error.localizedDescription)")
            return
        }
        let process = Process()
        process.executableURL = layout.python
        process.arguments = Self.arguments(for: layout)
        process.currentDirectoryURL = Self.dataDir
        process.environment = Self.environment(base: ProcessInfo.processInfo.environment,
                                               dataDir: Self.dataDir)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = log
        process.standardError = log
        process.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            Task { @MainActor in self?.didExit(status: status) }
        }
        do {
            try process.run()
        } catch {
            try? log.close()
            fail("Couldn't start the bundled daemon: \(error.localizedDescription)")
            return
        }
        try? log.close()  // the child keeps its own descriptor
        self.process = process
        startedAt = Date()
        try? String(process.processIdentifier).write(to: Self.pidFile, atomically: true, encoding: .utf8)
        state = .running(pid: process.processIdentifier)
    }

    private func didExit(status: Int32) {
        process = nil
        try? FileManager.default.removeItem(at: Self.pidFile)
        guard !stopping else {
            state = .stopped
            return
        }
        if Date().timeIntervalSince(startedAt) >= Self.healthyUptime {
            quickFailures = 0
        }
        quickFailures += 1
        let tail = Self.logTail()
        if quickFailures > Self.maxQuickFailures {
            let reason = tail.contains("address already in use")
                ? "Port \(Self.port) is in use by another program, so the assistant can't start. Quit that program, then try again."
                : "The assistant stopped \(Self.maxQuickFailures) times in a row (exit \(status))."
            fail(reason + (tail.isEmpty ? "" : "\n\nLast lines of the log:\n" + tail))
            return
        }
        let delay = Self.backoff(afterFailures: quickFailures)
        state = .restarting(attempt: quickFailures, delay: delay)
        restartTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay) * 1_000_000_000)
            guard !Task.isCancelled else { return }
            await self?.startIfNoDaemon()
        }
    }

    private func fail(_ message: String) {
        state = .failed(message)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "bot-inator's assistant couldn't start"
        alert.informativeText = message
        alert.addButton(withTitle: "Try Again")
        alert.addButton(withTitle: "Show Log")
        alert.addButton(withTitle: "Close")
        NSApp.activate(ignoringOtherApps: true)
        switch alert.runModal() {
        case .alertFirstButtonReturn: retry()
        case .alertSecondButtonReturn: showLog()
        default: break
        }
    }

    // MARK: helpers

    static func healthAnswers(timeout: TimeInterval = 2) async -> Bool {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = timeout
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        let session = URLSession(configuration: config)
        defer { session.finishTasksAndInvalidate() }
        guard let (_, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse else { return false }
        return http.statusCode == 200
    }

    /// The pid in our pid file, if that process is still our bundled Python.
    private static func ourOrphan(python: URL) -> pid_t? {
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0, kill(pid, 0) == 0 else { return nil }
        var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return nil }
        let path = URL(fileURLWithPath: String(cString: buffer)).resolvingSymlinksInPath().path
        return path == python.resolvingSymlinksInPath().path ? pid : nil
    }

    private static func waitForExit(pid: pid_t, seconds: TimeInterval) {
        let deadline = Date().addingTimeInterval(seconds)
        while kill(pid, 0) == 0 && Date() < deadline {
            usleep(50_000)
        }
        if kill(pid, 0) == 0 {
            kill(pid, SIGKILL)
        }
    }

    /// Append to the log; start a new one past 10 MB (one old copy kept).
    private static func openLog() throws -> FileHandle {
        let fm = FileManager.default
        let url = logURL
        if let size = try? fm.attributesOfItem(atPath: url.path)[.size] as? Int, size > 10_000_000 {
            let old = url.appendingPathExtension("1")
            try? fm.removeItem(at: old)
            try? fm.moveItem(at: url, to: old)
        }
        if !fm.fileExists(atPath: url.path) {
            fm.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        handle.seekToEndOfFile()
        handle.write(Data("\n==> \(Date()) starting the bundled daemon\n".utf8))
        return handle
    }

    /// The last few lines of the log, for the failure message.
    static func logTail(lines: Int = 6) -> String {
        guard let handle = try? FileHandle(forReadingFrom: logURL) else { return "" }
        defer { try? handle.close() }
        let size = handle.seekToEndOfFile()
        handle.seek(toFileOffset: size > 4096 ? size - 4096 : 0)
        let text = String(decoding: handle.readDataToEndOfFile(), as: UTF8.self)
        return text.split(separator: "\n").suffix(lines).joined(separator: "\n")
    }
}
