#!/usr/bin/env swift
// macOS Menu Bar widget for Solar Monitor
// Run: swift SolarWidget.swift

import Cocoa

class MenuTarget: NSObject {
    @objc func noop(_ sender: Any?) {}
}

let menuTarget = MenuTarget()

func styledItem(_ text: String, bold: Bool = false, color: NSColor = .labelColor, size: CGFloat = 13) -> NSMenuItem {
    let item = NSMenuItem(title: text, action: #selector(MenuTarget.noop(_:)), keyEquivalent: "")
    item.target = menuTarget
    let font = bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.systemFont(ofSize: size)
    item.attributedTitle = NSAttributedString(
        string: text,
        attributes: [.font: font, .foregroundColor: color]
    )
    return item
}

func headerItem(_ text: String) -> NSMenuItem {
    return styledItem(text, bold: true, color: .secondaryLabelColor, size: 11)
}

class SolarMenuBar: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?
    let dataPath: String

    override init() {
        let scriptDir = URL(fileURLWithPath: #file).deletingLastPathComponent().deletingLastPathComponent()
        self.dataPath = scriptDir.appendingPathComponent("widget_data.json").path
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateDisplay()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.updateDisplay()
        }
    }

    func updateDisplay() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: dataPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            statusItem.button?.title = "☀️ ---%"
            buildMenu(nil)
            return
        }

        let soc = json["soc"] as? Double ?? 0
        let isCharging = json["is_charging"] as? Bool ?? false

        let battIcon: String
        switch soc {
        case 80...: battIcon = "🔋"
        case 50..<80: battIcon = "🔋"
        case 20..<50: battIcon = "🪫"
        default: battIcon = "⚠️"
        }

        let chargeIcon = isCharging ? "⚡" : ""
        statusItem.button?.title = "\(battIcon)\(chargeIcon) \(Int(soc))%"

        buildMenu(json)
    }

    func buildMenu(_ json: [String: Any]?) {
        let menu = NSMenu()

        if let json = json {
            let soc = json["soc"] as? Double ?? 0
            let pvPower = json["pv_power"] as? Double ?? 0
            let loadPower = json["load_power"] as? Double ?? 0
            let battPower = json["battery_power"] as? Double ?? 0
            let isCharging = json["is_charging"] as? Bool ?? false

            // Current status
            menu.addItem(headerItem("CURRENT STATUS"))

            let socColor: NSColor = soc >= 50 ? .systemGreen : soc >= 30 ? .systemOrange : .systemRed
            menu.addItem(styledItem("  Battery: \(Int(soc))%", bold: true, color: socColor, size: 14))
            menu.addItem(styledItem("  Solar:   \(Int(pvPower))W", color: .systemYellow))
            menu.addItem(styledItem("  Load:    \(Int(loadPower))W"))

            let battLabel = isCharging ? "↑ Charging" : "↓ Draining"
            let battColor: NSColor = isCharging ? .systemGreen : .systemOrange
            menu.addItem(styledItem("  Battery: \(Int(battPower))W \(battLabel)", color: battColor))

            menu.addItem(NSMenuItem.separator())

            // Forecast
            if let forecast = json["forecast"] as? [String: Any] {
                let socAtSunrise = forecast["soc_at_sunrise"] as? Double ?? 0
                let hoursEmpty = forecast["hours_until_empty"] as? Double ?? 0
                let willDeplete = forecast["will_deplete"] as? Bool ?? false
                let drainRate = forecast["drain_rate_w"] as? Double ?? 0
                let hoursSunrise = forecast["hours_until_sunrise"] as? Double ?? 0
                let riskLevel = forecast["risk_level"] as? String ?? "ok"

                menu.addItem(headerItem("FORECAST"))

                let riskColor: NSColor
                switch riskLevel {
                case "critical": riskColor = .systemRed
                case "warning": riskColor = .systemOrange
                case "watch": riskColor = .systemYellow
                default: riskColor = .systemGreen
                }
                menu.addItem(styledItem("  Risk: \(riskLevel.uppercased())", bold: true, color: riskColor, size: 14))

                let hoursStr: String
                if hoursEmpty > 100 {
                    hoursStr = "99+ hours"
                } else {
                    let h = Int(hoursEmpty)
                    let m = Int((hoursEmpty - Double(h)) * 60)
                    hoursStr = "\(h)h \(m)m"
                }
                menu.addItem(styledItem("  Hours left:     \(hoursStr)"))
                menu.addItem(styledItem("  Drain rate:     \(Int(drainRate))W"))
                menu.addItem(styledItem("  SOC at sunrise: \(Int(socAtSunrise))%"))
                menu.addItem(styledItem("  Sunrise in:     \(String(format: "%.1f", hoursSunrise))h"))

                if willDeplete {
                    menu.addItem(NSMenuItem.separator())
                    menu.addItem(styledItem("  ⚠️  BATTERY WILL RUN OUT", bold: true, color: .systemRed, size: 14))
                }
            }

            // Weather
            if let weather = json["weather"] as? [String: Any] {
                menu.addItem(NSMenuItem.separator())
                menu.addItem(headerItem("WEATHER"))

                let temp = weather["temp"] as? Double ?? 0
                let clouds = weather["clouds"] as? Double ?? 0
                let desc = weather["description"] as? String ?? ""
                menu.addItem(styledItem("  \(desc.capitalized), \(Int(temp))°C"))
                menu.addItem(styledItem("  Clouds: \(Int(clouds))%", color: clouds > 70 ? .systemOrange : .labelColor))
            }

            // Updated
            if let updated = json["updated"] as? String {
                menu.addItem(NSMenuItem.separator())
                let short = String(updated.prefix(19)).replacingOccurrences(of: "T", with: " ")
                menu.addItem(styledItem(short, color: .tertiaryLabelColor, size: 11))
            }
        } else {
            menu.addItem(styledItem("No data available"))
            menu.addItem(styledItem("Run: python -m solar_monitor", color: .secondaryLabelColor, size: 11))
        }

        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))

        statusItem.menu = menu
    }
}

let app = NSApplication.shared
let delegate = SolarMenuBar()
app.delegate = delegate
app.run()
