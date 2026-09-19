// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "VideoStudioPublishApproval",
    platforms: [.macOS(.v13)],
    products: [
        .executable(
            name: "video-studio-publish-approve",
            targets: ["VideoStudioPublishApproval"]
        ),
    ],
    targets: [
        .target(
            name: "PublishApprovalCore",
            linkerSettings: [
                .linkedFramework("LocalAuthentication"),
                .linkedFramework("Security"),
            ]
        ),
        .executableTarget(
            name: "VideoStudioPublishApproval",
            dependencies: ["PublishApprovalCore"],
            linkerSettings: [.linkedFramework("AppKit")]
        ),
        .testTarget(
            name: "PublishApprovalCoreTests",
            dependencies: ["PublishApprovalCore"]
        ),
    ]
)
