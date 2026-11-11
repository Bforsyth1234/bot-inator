import SwiftUI

/// One row of `GET /api/outcomes/stats` "sources" (#79).
struct OutcomeSourceRow: Codable, Hashable, Identifiable {
    var id: String { source }
    let source: String
    let shown: Int
    let approved: Int
    let declined: Int
    let timedOut: Int
    let kept: Int
    let notKept: Int
    let approvalRate: Double?
    let keptRate: Double?
    let thresholdBump: Int?

    enum CodingKeys: String, CodingKey {
        case source, shown, approved, declined, kept
        case timedOut = "timed_out"
        case notKept = "not_kept"
        case approvalRate = "approval_rate"
        case keptRate = "kept_rate"
        case thresholdBump = "threshold_bump"
    }
}

private struct OutcomeStats: Codable {
    let sources: [OutcomeSourceRow]
}

/// One row of `GET /api/outcomes/removal-candidates`.
struct RemovalCandidate: Codable, Hashable, Identifiable {
    var id: String { toolName }
    let toolName: String
    let source: String?
    let daysUnused: Int

    enum CodingKeys: String, CodingKey {
        case source
        case toolName = "tool_name"
        case daysUnused = "days_unused"
    }
}

@MainActor
final class SuggestionOutcomesViewModel: ObservableObject {
    @Published private(set) var sources: [OutcomeSourceRow] = []
    @Published private(set) var candidates: [RemovalCandidate] = []
    @Published private(set) var loaded = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func refresh() async {
        do {
            let stats: OutcomeStats = try await get("api/outcomes/stats")
            let removals: [RemovalCandidate] = try await get("api/outcomes/removal-candidates")
            sources = stats.sources
            candidates = removals
            loaded = true
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// "Keep": stop listing this unused tool.
    func keep(_ candidate: RemovalCandidate) async {
        do {
            let path = "api/outcomes/removal-candidates/\(candidate.toolName)/keep"
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent(path), method: "POST"
            )
            let (_, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            candidates.removeAll { $0.toolName == candidate.toolName }
            errorMessage = nil
        } catch {
            errorMessage = "Keep failed: " + error.localizedDescription
        }
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let request = try DaemonAuth.request(baseURL.appendingPathComponent(path))
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw NSError(domain: "SuggestionOutcomes", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

/// "Suggestion outcomes" for the Debug tab (#79): how each idea source's
/// suggestions fared, and installed tools that were never used.
struct SuggestionOutcomesView: View {
    @StateObject private var viewModel = SuggestionOutcomesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Suggestion outcomes")
                    .appFont(.secondary, weight: .semibold)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Refresh") { Task { await viewModel.refresh() } }
                    .buttonStyle(.borderless)
                    .appFont(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if viewModel.loaded && viewModel.sources.isEmpty {
                Text("No suggestions shown yet.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            ForEach(viewModel.sources) { row in
                HStack(alignment: .top) {
                    Text(row.source).appFont(.secondary).foregroundStyle(.secondary)
                    Spacer()
                    Text(Self.summary(row))
                        .appFont(.secondary, design: .monospaced)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
            }
            if !viewModel.candidates.isEmpty {
                Text("Installed but never used (30+ days)")
                    .appFont(.secondary, weight: .semibold)
                    .padding(.top, 4)
                ForEach(viewModel.candidates) { candidate in
                    HStack {
                        Text(candidate.toolName).appFont(.secondary, design: .monospaced)
                        Text("\(candidate.daysUnused) days")
                            .appFont(.fine)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("Keep") { Task { await viewModel.keep(candidate) } }
                            .buttonStyle(.borderless)
                            .appFont(.secondary)
                            .help("Stop listing this tool. Remove it in Manage Tools instead if you don't want it.")
                    }
                }
            }
            Divider()
        }
        .task { await viewModel.refresh() }
    }

    private static func percent(_ value: Double?) -> String {
        guard let value else { return "–" }
        return "\(Int((value * 100).rounded()))%"
    }

    private static func summary(_ row: OutcomeSourceRow) -> String {
        var text = "\(row.shown) shown · \(row.approved) yes · \(row.declined) no"
        text += " · approved \(percent(row.approvalRate)) · kept \(percent(row.keptRate))"
        if let bump = row.thresholdBump, bump > 0 { text += " · bar +\(bump)" }
        return text
    }
}
