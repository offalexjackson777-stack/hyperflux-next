// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    Assessment, AssessmentFinding, AssessmentState, ComponentAssessment, ComponentId,
    ComponentState, DriverState, render_doctor_text, render_status_text,
};
use std::collections::BTreeMap;

fn frozen_ready_assessment() -> Assessment {
    Assessment {
        schema: "hyperflux-system-assessment-v2".to_owned(),
        state: AssessmentState::Ready,
        package_version: "2.0.0".to_owned(),
        components: healthy_components(),
        driver: DriverState::Active,
        bridge_ready: true,
        bridge_responding: true,
        receiver_generations: 1,
        qualified_generations: 1,
        working: vec![
            "the installed driver is active".to_owned(),
            "the bridge is serving one qualified receiver".to_owned(),
        ],
        findings: Vec::new(),
        next_action: "no action required".to_owned(),
    }
}

fn frozen_needs_attention_assessment() -> Assessment {
    Assessment {
        schema: "hyperflux-system-assessment-v2".to_owned(),
        state: AssessmentState::NeedsAttention,
        package_version: "2.0.0".to_owned(),
        components: vec![
            ComponentAssessment {
                component: ComponentId::Core,
                state: ComponentState::NeedsAttention,
                required: true,
                installed: true,
                version: Some("2.0.0".to_owned()),
            },
            ComponentAssessment {
                component: ComponentId::Full,
                state: ComponentState::Ready,
                required: false,
                installed: true,
                version: Some("2.0.0".to_owned()),
            },
        ],
        driver: DriverState::Active,
        bridge_ready: false,
        bridge_responding: false,
        receiver_generations: 0,
        qualified_generations: 0,
        working: vec!["the installed driver is active".to_owned()],
        findings: vec![AssessmentFinding {
            code: "HFX-SERVICE-001".to_owned(),
            severity: "error".to_owned(),
            technical_cause: "The local bridge socket did not provide a usable snapshot."
                .to_owned(),
            user_explanation: "The bridge is not ready.".to_owned(),
            safe_action: "Verify the bridge service without changing device state.".to_owned(),
            verification: "Run Doctor again and confirm the bridge is ready.".to_owned(),
            documentation: "docs/operators/troubleshooting.md".to_owned(),
            details: BTreeMap::new(),
        }],
        next_action: "Verify the bridge service without changing device state.".to_owned(),
    }
}

fn healthy_components() -> Vec<ComponentAssessment> {
    vec![
        ComponentAssessment {
            component: ComponentId::Core,
            state: ComponentState::Ready,
            required: true,
            installed: true,
            version: Some("2.0.0".to_owned()),
        },
        ComponentAssessment {
            component: ComponentId::Full,
            state: ComponentState::Ready,
            required: false,
            installed: true,
            version: Some("2.0.0".to_owned()),
        },
    ]
}

#[test]
fn release_2_cli_text_and_json_surfaces_are_byte_golden() {
    let ready = frozen_ready_assessment();
    let attention = frozen_needs_attention_assessment();

    assert_eq!(
        render_doctor_text(&ready, false),
        include_str!("../../../tests/fixtures/cli/hyperfluxctl-doctor-ready.txt")
    );
    assert_eq!(
        render_status_text(&ready),
        include_str!("../../../tests/fixtures/cli/hyperfluxctl-status-ready.txt")
    );
    assert_eq!(
        render_doctor_text(&attention, false),
        include_str!("../../../tests/fixtures/cli/hyperfluxctl-doctor-needs-attention.txt")
    );
    assert_eq!(
        serde_json::to_string_pretty(&ready).expect("ready assessment serializes") + "\n",
        include_str!("../../../tests/fixtures/cli/hyperfluxctl-ready.json")
    );
    assert_eq!(
        serde_json::to_string_pretty(&attention).expect("assessment serializes") + "\n",
        include_str!("../../../tests/fixtures/cli/hyperfluxctl-needs-attention.json")
    );
}
