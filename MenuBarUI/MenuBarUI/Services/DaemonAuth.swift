import Foundation

/// Bearer token for the daemon's HTTP API and WebSocket (#27).
///
/// The daemon creates `~/.bot-inator/token` (0600) on first start. Clients
/// read it on every connect / request, since the daemon may create it
/// after the app starts. URLSession sends no `Origin` header, which the
/// daemon requires (it refuses anything that looks browser-originated).
enum DaemonAuth {
    static let missingTokenHint =
        "No API token at ~/.bot-inator/token. Start the daemon once to create it."
    static let rejectedTokenHint =
        "The daemon rejected the API token. Restart the daemon, then reopen this window."

    static var tokenURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".bot-inator/token")
    }

    /// The current token, or nil when the file is missing or empty.
    static func token() -> String? {
        guard let raw = try? String(contentsOf: tokenURL, encoding: .utf8) else {
            return nil
        }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }

    /// A request for `url` carrying `Authorization: Bearer <token>`.
    static func request(_ url: URL, method: String = "GET") throws -> URLRequest {
        guard let token = token() else { throw DaemonAuthError.missingToken }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    /// Throws `.rejected` on 401/403 so callers show the token hint.
    static func checkAuth(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else { return }
        if http.statusCode == 401 || http.statusCode == 403 {
            throw DaemonAuthError.rejected
        }
    }
}

enum DaemonAuthError: LocalizedError {
    case missingToken
    case rejected

    var errorDescription: String? {
        switch self {
        case .missingToken: return DaemonAuth.missingTokenHint
        case .rejected: return DaemonAuth.rejectedTokenHint
        }
    }
}
