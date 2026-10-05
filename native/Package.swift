// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Threadgoon",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "Threadgoon", targets: ["Threadgoon"])],
    targets: [
        .target(name: "ThreadgoonCore"),
        .executableTarget(name: "Threadgoon", dependencies: ["ThreadgoonCore"]),
        .testTarget(name: "ThreadgoonCoreTests", dependencies: ["ThreadgoonCore", "Threadgoon"]),
    ]
)
