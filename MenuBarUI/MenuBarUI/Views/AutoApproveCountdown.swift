import AppKit
import SwiftUI

/// #117: an approval card that approves itself after `auto_approve_seconds`
/// unless the user touches it. Any touch (hover, edit, tick, click, focus,
/// key press, Deny) stops it for good; the card then waits like any other.
/// `onFire` runs at most once, and never after `stop()`, so a closed card
/// can't send a second response.
@MainActor
final class AutoApproveCountdown: ObservableObject {
    let total: Double
    @Published private(set) var remaining: Double
    @Published private(set) var isRunning = false

    /// Called on every tick; returns true when the user touched the card in
    /// a way the SwiftUI view can't see (the presenter checks the window).
    var probe: (() -> Bool)?

    private var timer: Timer?
    private var onFire: (() -> Void)?
    private var deadline = Date()
    private var stopped = false

    /// nil unless `seconds` is a positive number.
    init?(seconds: Int?, onFire: @escaping () -> Void) {
        guard let seconds, seconds > 0 else { return nil }
        total = Double(seconds)
        remaining = Double(seconds)
        self.onFire = onFire
    }

    func start() {
        guard !stopped, timer == nil else { return }
        isRunning = true
        deadline = Date().addingTimeInterval(remaining)
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    /// Permanent: a stopped countdown never restarts and never fires.
    func stop() {
        stopped = true
        isRunning = false
        timer?.invalidate()
        timer = nil
        onFire = nil
        probe = nil
    }

    private func tick() {
        guard !stopped else { return }
        if probe?() == true {
            stop()
            return
        }
        remaining = max(0, deadline.timeIntervalSinceNow)
        if remaining <= 0 {
            let fire = onFire
            stop()
            fire?()
        }
    }
}

/// "Approving in 10s" with a shrinking bar. Hidden once stopped.
struct AutoApproveBar: View {
    @ObservedObject var countdown: AutoApproveCountdown

    var body: some View {
        if countdown.isRunning {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Image(systemName: "timer")
                    Text("Approving in \(Int(countdown.remaining.rounded(.up)))s")
                        .appFont(.secondary, weight: .semibold)
                        .monospacedDigit()
                    Spacer()
                    Text("Touch the card to stop")
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
                .foregroundStyle(.orange)
                ProgressView(value: countdown.remaining, total: countdown.total)
                    .progressViewStyle(.linear)
                    .tint(.orange)
                    .animation(.linear(duration: 0.1), value: countdown.remaining)
            }
            .accessibilityElement(children: .combine)
        }
    }
}

extension View {
    /// Stops `countdown` when the pointer enters the card or a key is pressed.
    func stopsCountdown(_ countdown: AutoApproveCountdown?) -> some View {
        self
            .onHover { inside in if inside { countdown?.stop() } }
            .onKeyPress(phases: .down) { _ in
                countdown?.stop()
                return .ignored
            }
    }
}
