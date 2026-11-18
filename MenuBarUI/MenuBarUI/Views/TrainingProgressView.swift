import SwiftUI

/// Personal model training progress (#119): progress bar, state, time left
/// and Cancel. The run starts only after the user approves the training
/// card; it waits until the Mac is plugged in and the user is away, and
/// pauses when they come back.
///
/// Routes: `GET /api/training/status` (polled while a run is going),
/// `POST /api/training/cancel`. A `training_progress` WebSocket message
/// (same fields as the status route) can be fed in with
/// `TrainingProgressViewModel.apply(_:)` or the `status:` initialiser.

struct TrainingRunStatus: Codable, Equatable {
    let state: String
    let progress: Double
    let iteration: Int
    let total: Int
    let etaS: Int?
    let loss: Double?
    let adapterDir: String?
    let error: String?
    let reason: String?
    let usable: Int?
    let negatives: Int?

    enum CodingKeys: String, CodingKey {
        case state, progress, iteration, total, loss, error, reason, usable, negatives
        case etaS = "eta_s"
        case adapterDir = "adapter_dir"
    }

    init(state: String, progress: Double = 0, iteration: Int = 0, total: Int = 0,
         etaS: Int? = nil, loss: Double? = nil, adapterDir: String? = nil,
         error: String? = nil, reason: String? = nil, usable: Int? = nil, negatives: Int? = nil) {
        self.state = state
        self.progress = progress
        self.iteration = iteration
        self.total = total
        self.etaS = etaS
        self.loss = loss
        self.adapterDir = adapterDir
        self.error = error
        self.reason = reason
        self.usable = usable
        self.negatives = negatives
    }

    var isActive: Bool { ["waiting_for_idle", "running", "paused"].contains(state) }
}

@MainActor
final class TrainingProgressViewModel: ObservableObject {
    @Published private(set) var status: TrainingRunStatus?
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession
    private let polls: Bool

    /// `status:` injects a status (previews, or a WS-driven parent); with
    /// `polls: false` the view never calls the daemon on its own.
    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared,
         status: TrainingRunStatus? = nil,
         polls: Bool = true) {
        self.baseURL = baseURL
        self.session = session
        self.status = status
        self.polls = polls
    }

    /// Feed a `training_progress` WebSocket payload.
    func apply(_ newStatus: TrainingRunStatus) {
        status = newStatus
    }

    private func send(_ path: String, method: String = "GET") async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        let request = try DaemonAuth.request(url, method: method)
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = json?["detail"] as? String
            throw NSError(domain: "TrainingProgress", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        guard polls else { return }
        do {
            status = try JSONDecoder().decode(TrainingRunStatus.self,
                                              from: try await send("/api/training/status"))
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't read training status: \(error.localizedDescription)"
        }
    }

    /// Poll every 2 s while a run is active, every 30 s otherwise.
    func pollLoop() async {
        guard polls else { return }
        while !Task.isCancelled {
            await refresh()
            let seconds: UInt64 = (status?.isActive ?? false) ? 2 : 30
            try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)
        }
    }

    func cancel() async {
        isBusy = true
        defer { isBusy = false }
        do {
            status = try JSONDecoder().decode(TrainingRunStatus.self,
                                              from: try await send("/api/training/cancel", method: "POST"))
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't cancel: \(error.localizedDescription)"
        }
    }
}

/// Self-contained "Personal model" progress section: embed with
/// `TrainingProgressView()`; hidden-friendly (shows a one-liner when idle).
struct TrainingProgressView: View {
    @StateObject private var viewModel: TrainingProgressViewModel
    @State private var confirmCancel = false

    init(viewModel: TrainingProgressViewModel? = nil) {
        _viewModel = StateObject(wrappedValue: viewModel ?? TrainingProgressViewModel())
    }

    init(status: TrainingRunStatus) {
        _viewModel = StateObject(wrappedValue: TrainingProgressViewModel(status: status, polls: false))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "cpu").foregroundStyle(.secondary)
                Text("Personal model").appFont(.headline)
                Spacer()
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
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.pollLoop() }
        .confirmationDialog("Cancel training?", isPresented: $confirmCancel) {
            Button("Cancel training", role: .destructive) { Task { await viewModel.cancel() } }
            Button("Keep training", role: .cancel) {}
        } message: {
            Text("What it has learned so far is thrown away.")
        }
    }

    @ViewBuilder
    private func content(_ status: TrainingRunStatus) -> some View {
        Text(stateText(status))
            .appFont(.body)
            .fixedSize(horizontal: false, vertical: true)
        if status.isActive || status.state == "done" {
            ProgressView(value: min(max(status.progress, 0), 1))
                .progressViewStyle(.linear)
            HStack {
                if status.total > 0 {
                    Text("Step \(status.iteration) of \(status.total)")
                }
                Spacer()
                if status.isActive, let eta = status.etaS {
                    Text(etaText(eta))
                }
            }
            .appFont(.fine)
            .foregroundStyle(.secondary)
        }
        if status.isActive {
            HStack {
                Spacer()
                Button("Cancel", role: .destructive) { confirmCancel = true }
                    .controlSize(.small)
                    .disabled(viewModel.isBusy)
            }
        }
    }

    private func stateText(_ status: TrainingRunStatus) -> String {
        switch status.state {
        case "waiting_for_idle":
            return "Waiting to start: \(status.reason ?? "it runs while you're away and the Mac is plugged in")."
        case "running":
            return "Training on this Mac. It pauses when you come back."
        case "paused":
            return "Paused (\(status.reason ?? "you're using the Mac")); it will resume when you're away."
        case "done":
            return "Done. Your model is being checked against the original."
        case "failed":
            return "Training failed: \(status.error ?? "unknown error")."
        case "cancelled":
            return "Cancelled. Nothing was kept."
        default:
            if let usable = status.usable {
                return "Not training. \(usable) decisions stored so far."
            }
            return "Not training."
        }
    }

    private func etaText(_ seconds: Int) -> String {
        if seconds < 60 { return "Less than a minute left" }
        let minutes = Int((Double(seconds) / 60).rounded())
        if minutes < 90 { return "About \(minutes) min left" }
        return String(format: "About %.1f h left", Double(seconds) / 3600)
    }
}
