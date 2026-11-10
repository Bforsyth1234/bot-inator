import SwiftUI

/// The app-wide "Text size" preference (#62). A per-user UI setting kept in
/// `UserDefaults` (no daemon round-trip).
///
/// macOS ignores `.dynamicTypeSize` for the built-in text styles (`.caption`
/// is 10 pt whatever the value), so the setting is applied explicitly: each
/// view names a *role* with `.appFont(...)`, and the role resolves to a
/// system text style for the current size. Medium is the default.
enum TextSize: String, CaseIterable, Identifiable {
    case small, medium, large

    static let storageKey = "textSize"
    static let fallback: TextSize = .medium

    var id: String { rawValue }

    var label: String {
        switch self {
        case .small: return "Small"
        case .medium: return "Medium"
        case .large: return "Large"
        }
    }

    /// Multiplier for fixed layout metrics that sit next to text (label
    /// columns, minimum editor heights, window frames).
    var layoutScale: CGFloat {
        switch self {
        case .small: return 0.92
        case .medium: return 1.0
        case .large: return 1.15
        }
    }
}

/// What a piece of text is for. Views pick a role; `TextSize` picks the style.
enum AppFontRole {
    /// Timestamps, badges, the densest rows.
    case fine
    /// Secondary/explanatory text and section captions.
    case secondary
    /// Sub-titles under a headline.
    case label
    /// Slightly de-emphasised running text.
    case callout
    /// Running text.
    case body
    case headline
    /// Icons and titles one step above body.
    case title3
    /// Header icons.
    case title2

    //                         Small          Medium        Large
    // fine      (macOS pt)    caption2 10    subheadline 11  callout 12
    // secondary               caption 10     callout 12      body 13
    // label                   subheadline 11 body 13         title3 15
    // callout                 callout 12     body 13         title3 15
    // body                    body 13        body 13         title3 15
    // headline                headline 13    headline 13     title3 15 (semibold)
    // title3                  title3 15      title3 15       title2 17
    // title2                  title2 17      title2 17       title 22
    func style(for size: TextSize) -> Font.TextStyle {
        switch (self, size) {
        case (.fine, .small): return .caption2
        case (.fine, .medium): return .subheadline
        case (.fine, .large): return .callout
        case (.secondary, .small): return .caption
        case (.secondary, .medium): return .callout
        case (.secondary, .large): return .body
        case (.label, .small): return .subheadline
        case (.label, .medium): return .body
        case (.label, .large): return .title3
        case (.callout, .small): return .callout
        case (.callout, .medium): return .body
        case (.callout, .large): return .title3
        case (.body, .small), (.body, .medium): return .body
        case (.body, .large): return .title3
        case (.headline, .small), (.headline, .medium): return .headline
        case (.headline, .large): return .title3
        case (.title3, .small), (.title3, .medium): return .title3
        case (.title3, .large): return .title2
        case (.title2, .small), (.title2, .medium): return .title2
        case (.title2, .large): return .title
        }
    }

    func font(for size: TextSize,
              design: Font.Design = .default,
              weight: Font.Weight? = nil) -> Font {
        let base = Font.system(style(for: size), design: design)
        if let weight { return base.weight(weight) }
        // `.headline` is bold by definition; keep that when Large maps it
        // to a title style.
        if self == .headline, size == .large { return base.weight(.semibold) }
        return base
    }
}

private struct TextSizeKey: EnvironmentKey {
    static let defaultValue: TextSize = TextSize.fallback
}

extension EnvironmentValues {
    /// The current "Text size" setting; set by `appTextSize()` at each root.
    var textSize: TextSize {
        get { self[TextSizeKey.self] }
        set { self[TextSizeKey.self] = newValue }
    }
}

private struct AppFontModifier: ViewModifier {
    @Environment(\.textSize) private var textSize
    let role: AppFontRole
    let design: Font.Design
    let weight: Font.Weight?

    func body(content: Content) -> some View {
        content.font(role.font(for: textSize, design: design, weight: weight))
    }
}

/// Attached at every window/scene root: reads the stored setting, publishes
/// it to the environment, and makes it the default font so text with no
/// explicit `.appFont` (buttons, form rows, plain `Text`) scales too.
private struct AppTextSizeRoot: ViewModifier {
    @AppStorage(TextSize.storageKey) private var stored: String = TextSize.fallback.rawValue

    func body(content: Content) -> some View {
        let size = TextSize(rawValue: stored) ?? TextSize.fallback
        content
            .font(AppFontRole.body.font(for: size))
            .environment(\.textSize, size)
    }
}

extension View {
    /// Use instead of `.font(...)` so the text follows the Text size setting.
    func appFont(_ role: AppFontRole,
                 design: Font.Design = .default,
                 weight: Font.Weight? = nil) -> some View {
        modifier(AppFontModifier(role: role, design: design, weight: weight))
    }

    /// Apply the Text size setting to a window or panel root.
    func appTextSize() -> some View {
        modifier(AppTextSizeRoot())
    }
}

/// The Settings row for the preference.
struct TextSizePicker: View {
    @AppStorage(TextSize.storageKey) private var stored: String = TextSize.fallback.rawValue

    var body: some View {
        Picker("Text size", selection: $stored) {
            ForEach(TextSize.allCases) { size in
                Text(size.label).tag(size.rawValue)
            }
        }
        .pickerStyle(.segmented)
        Text("Applies to every window and the menu bar panel right away.")
            .appFont(.secondary)
            .foregroundStyle(.secondary)
    }
}
