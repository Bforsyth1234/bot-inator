import SwiftUI

/// Settings → Personal model (#120): the trained models, their scores
/// against the original, which one is in use, and "Use base model".
///
/// A model can be used only when it beat the original on the check after
/// training and was trained on the model the app runs now. It works only
/// with the local engine. None of this changes how approval cards work.
///
/// Routes: `GET /api/personal_model`, `POST /api/personal_model/use {n}`,
/// `POST /api/personal_model/use_base`.

struct PersonalAdapter: Codable, Identifiable, Equatable {
    let n: Int
    let createdAt: Double?
    let baseModel: String
    let rows: Int?
    let verdict: String?
    let cardLines: [String]
    let stale: Bool
    let usable: Bool
    let reason: String?

    var id: Int { n }

    enum CodingKeys: String, CodingKey {
        case n, rows, verdict, stale, usable, reason
        case createdAt = "created_at"
        case baseModel = "base_model"
        case cardLines = "card_lines"
    }
}

struct PersonalModelStatus: Codable, Equatable {
    let adapters: [PersonalAdapter]
    let active: Int?
    let engineOk: Bool
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case adapters, active, reason
        case engineOk = "engine_ok"
    }
}

@MainActor
final class PersonalModelViewModel: ObservableObject {
    @Published private(set) var status: PersonalModelStatus?
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared,
         status: PersonalModelStatus? = nil) {
        self.baseURL = baseURL
        self.session = session
        self.status = status
    }

    private func send(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        var request = try DaemonAuth.request(url, method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = json?["detail"] as? String
            throw NSError(domain: "PersonalModel", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        do {
            status = try JSONDecoder().decode(PersonalModelStatus.self,
                                              from: try await send("/api/personal_model"))
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't read your models: \(error.localizedDescription)"
        }
    }

    func use(_ adapter: PersonalAdapter) async {
        await change("/api/personal_model/use", body: ["n": adapter.n], failure: "Couldn't switch")
    }

    func useBase() async {
        await change("/api/personal_model/use_base", body: nil, failure: "Couldn't switch back")
    }

    private func change(_ path: String, body: [String: Any]?, failure: String) async {
        isBusy = true
        defer { isBusy = false }
        do {
            status = try JSONDecoder().decode(PersonalModelStatus.self,
                                              from: try await send(path, method: "POST", body: body))
            errorMessage = nil
        } catch {
            errorMessage = "\(failure): \(error.localizedDescription)"
        }
    }
}

/// Self-contained section: embed with `PersonalModelView()`.
struct PersonalModelView: View {
    @StateObject private var viewModel: PersonalModelViewModel

    init(viewModel: PersonalModelViewModel? = nil) {
        _viewModel = StateObject(wrappedValue: viewModel ?? PersonalModelViewModel())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.crop.circle.badge.checkmark").foregroundStyle(.secondary)
                Text("Your models").appFont(.headline)
                Spacer()
                Button {
                    Task { await viewModel.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh")
            }
            if let status = viewModel.status {
                content(status)
            } else {
                Text("Loading…").appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func content(_ status: PersonalModelStatus) -> some View {
        HStack {
            if status.active == nil {
                Label("Using the original model", systemImage: "checkmark.circle")
                    .appFont(.body)
            } else {
                Label("Using your model #\(status.active ?? 0)", systemImage: "checkmark.circle.fill")
                    .appFont(.body)
            }
            Spacer()
            if status.active != nil {
                Button("Use base model") { Task { await viewModel.useBase() } }
                    .controlSize(.small)
                    .disabled(viewModel.isBusy)
            }
        }
        if !status.engineOk, let reason = status.reason {
            Label(reason, systemImage: "info.circle")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if status.adapters.isEmpty {
            Text("No trained models yet.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else {
            ForEach(status.adapters) { adapter in
                row(adapter, status: status)
                if adapter.id != status.adapters.last?.id { Divider() }
            }
        }
    }

    @ViewBuilder
    private func row(_ adapter: PersonalAdapter, status: PersonalModelStatus) -> some View {
        let isActive = status.active == adapter.n
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text("Model #\(adapter.n)").appFont(.body).bold()
                if let created = adapter.createdAt {
                    Text(Date(timeIntervalSince1970: created), style: .date)
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
                if isActive {
                    Text("In use").appFont(.fine).foregroundStyle(.green)
                }
                Spacer()
                if adapter.usable && !isActive {
                    Button("Use my model") { Task { await viewModel.use(adapter) } }
                        .controlSize(.small)
                        .disabled(viewModel.isBusy || !status.engineOk)
                }
            }
            ForEach(Array(adapter.cardLines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .appFont(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if adapter.stale {
                Label(adapter.reason ?? "Trained on an older base model. Retrain to use it.",
                      systemImage: "clock.arrow.circlepath")
                    .appFont(.fine)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !adapter.usable, let reason = adapter.reason {
                Text(reason)
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let rows = adapter.rows {
                Text("Trained on \(rows) of your decisions · \(adapter.baseModel)")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .padding(.vertical, 2)
    }
}
