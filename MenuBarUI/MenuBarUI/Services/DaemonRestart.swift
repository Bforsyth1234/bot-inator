import Foundation

/// #150: the one way the app asks the daemon to restart.
///
/// `whenIdle: true` (after a save that needs a restart) sends
/// `POST /api/restart?when=idle`: the daemon restarts now if no turn is
/// running and no approval card is open, otherwise as soon as both clear
/// (it waits on its side, so closing Settings doesn't cancel it).
/// `whenIdle: false` (the Restart Daemon button) restarts at once.
enum DaemonRestart {
    enum Outcome: Equatable {
        case now
        case pending
        /// 409: the daemon runs under `--reload` and can't restart itself.
        case unavailable
        case failed(String)
    }

    static func request(
        baseURL: URL, session: URLSession = .shared, whenIdle: Bool
    ) async -> Outcome {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("api/restart"),
            resolvingAgainstBaseURL: false
        )
        if whenIdle { components?.queryItems = [URLQueryItem(name: "when", value: "idle")] }
        guard let url = components?.url else { return .failed("bad daemon URL") }
        do {
            let request = try DaemonAuth.request(url, method: "POST")
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                if http.statusCode == 409 { return .unavailable }
                return .failed(obj?["detail"] as? String ?? "HTTP \(http.statusCode)")
            }
            // An older daemon answers {"status": "restarting"}: that is "now".
            return obj?["restart"] as? String == "pending" ? .pending : .now
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    /// Status line after a save, for each outcome.
    static func savedMessage(_ outcome: Outcome) -> String {
        switch outcome {
        case .now: return "Saved. Restarting…"
        case .pending: return "Saved. Restarting when the current task finishes."
        case .unavailable: return "Saved. Restart the daemon to apply."
        case .failed(let why): return "Saved. Restart failed (\(why)); restart the daemon to apply."
        }
    }
}
