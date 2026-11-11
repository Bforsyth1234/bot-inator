import AppKit
import Foundation

/// "A new version is out" notice (#96 decision 4), off by default.
///
/// When the user turns it on in Settings (or clicks "Check now"), the app
/// asks GitHub for the latest release: one `GET
/// https://api.github.com/repos/Bforsyth1234/bot-inator/releases/latest`
/// with `Accept` and `User-Agent: bot-inator` headers, no cookies, no
/// cache. It sends no user data and not even this app's version (the
/// comparison happens here); GitHub sees the request and the Mac's IP
/// address. With the switch on it checks at launch and once a day.
///
/// It runs in the app, not the daemon, so it works when the daemon is down
/// or won't start (when an update matters most). The privacy ledger
/// (`ai/egress_log.py`) lives in the daemon and records only the daemon's
/// own sends, so this request is not in it; Settings shows when it last ran
/// and the exact URL instead.
@MainActor
final class UpdateChecker: ObservableObject {
    static let shared = UpdateChecker()

    static let enabledKey = "checkForUpdates"
    static let notifiedKey = "updateNotifiedVersion"
    static let releasesPage = URL(string: "https://github.com/Bforsyth1234/bot-inator/releases")!
    static let latestReleaseAPI = URL(string: "https://api.github.com/repos/Bforsyth1234/bot-inator/releases/latest")!
    static let interval: TimeInterval = 24 * 60 * 60

    @Published private(set) var latestVersion: String?
    @Published private(set) var latestURL: URL?
    @Published private(set) var lastChecked: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var checking = false

    private var timer: Timer?

    private init() {}

    var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: Self.enabledKey) }  // false unless turned on
        set {
            UserDefaults.standard.set(newValue, forKey: Self.enabledKey)
            objectWillChange.send()
            newValue ? schedule() : unschedule()
            if newValue { Task { await check(userInitiated: false) } }
        }
    }

    var currentVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    var updateAvailable: Bool {
        guard let latestVersion else { return false }
        return Self.isNewer(latestVersion, than: currentVersion)
    }

    /// From `applicationDidFinishLaunching`: nothing unless the switch is on.
    func startIfEnabled() {
        guard enabled else { return }
        schedule()
        Task { await check(userInitiated: false) }
    }

    private func schedule() {
        unschedule()
        timer = Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { _ in
            Task { @MainActor in await UpdateChecker.shared.check(userInitiated: false) }
        }
    }

    private func unschedule() {
        timer?.invalidate()
        timer = nil
    }

    func check(userInitiated: Bool) async {
        guard !checking else { return }
        checking = true
        defer { checking = false }
        var request = URLRequest(url: Self.latestReleaseAPI)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("bot-inator", forHTTPHeaderField: "User-Agent")
        request.timeoutInterval = 15
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        do {
            let (data, response) = try await session.data(for: request)
            lastChecked = Date()
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 404 {
                lastError = "No release has been published yet."
                return
            }
            guard status == 200 else {
                lastError = "GitHub answered \(status)."
                return
            }
            let release = try JSONDecoder().decode(Release.self, from: data)
            latestVersion = Self.normalized(release.tagName)
            latestURL = release.htmlURL.flatMap(URL.init(string:)) ?? Self.releasesPage
            lastError = nil
        } catch {
            lastChecked = Date()
            lastError = error.localizedDescription
            return
        }
        if updateAvailable && !userInitiated { notifyOnce() }
    }

    /// One alert per new version from the automatic check; Settings always
    /// shows it too.
    private func notifyOnce() {
        guard let latestVersion,
              UserDefaults.standard.string(forKey: Self.notifiedKey) != latestVersion else { return }
        UserDefaults.standard.set(latestVersion, forKey: Self.notifiedKey)
        let alert = NSAlert()
        alert.messageText = "bot-inator \(latestVersion) is available"
        alert.informativeText = "You have \(currentVersion). Download the new version from the release page, then replace the app in Applications."
        alert.addButton(withTitle: "Open Release Page")
        alert.addButton(withTitle: "Later")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            NSWorkspace.shared.open(latestURL ?? Self.releasesPage)
        }
    }

    private struct Release: Decodable {
        let tagName: String
        let htmlURL: String?
        enum CodingKeys: String, CodingKey {
            case tagName = "tag_name"
            case htmlURL = "html_url"
        }
    }

    // MARK: version compare

    /// "v0.3.0" → "0.3.0".
    static func normalized(_ tag: String) -> String {
        let t = tag.trimmingCharacters(in: .whitespaces)
        return t.hasPrefix("v") || t.hasPrefix("V") ? String(t.dropFirst()) : t
    }

    /// Numeric compare of dotted versions; a "-suffix" (pre-release, e.g.
    /// "0.3.0-rc1") ranks below the same version without one.
    static func isNewer(_ candidate: String, than current: String) -> Bool {
        func parts(_ v: String) -> ([Int], Bool) {
            let s = normalized(v)
            let core = s.split(separator: "-", maxSplits: 1).first.map(String.init) ?? s
            let nums = core.split(separator: ".").map { Int($0.prefix { $0.isNumber }) ?? 0 }
            return (nums, s.contains("-"))
        }
        let (a, aPre) = parts(candidate)
        let (b, bPre) = parts(current)
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0
            let y = i < b.count ? b[i] : 0
            if x != y { return x > y }
        }
        return bPre && !aPre
    }
}
