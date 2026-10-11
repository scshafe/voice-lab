// "Goals — pre-registered experiments" (checklist P-04, P-05): the goals and the form.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import ".."

Card {
    id: card
    objectName: "goalsCard"
    heading: "Goals — pre-registered experiments"
    subheading: "Declare the metric, target, and sample size before you sing; the first N qualifying takes after creation decide the verdict — no optional stopping, no peeking. Textbook Part VII, running in production."

    // The SuiSelect stand-in assigns its own `value` when a choice is made, which drops
    // an owner's binding of `value`; so the selects are set from the model whenever the
    // form or its options change (the form empties after each submit).
    function syncSelects() {
        metricSelect.value = practice.formMetric
        directionSelect.value = practice.formDirection
        scopeSelect.value = practice.formCluster
    }
    Connections {
        target: practice
        function onFormChanged() { card.syncSelects() }
        function onGoalsChanged() { card.syncSelects() }
    }
    Component.onCompleted: syncSelects()

    SuiBanner {
        objectName: "goalsError"
        Layout.fillWidth: true
        visible: practice.goalsState === "failed"
        tone: "danger"
        title: "Goals did not load"
        text: practice.goalsError
    }

    Repeater {
        objectName: "goalRows"
        model: practice.goals
        delegate: ColumnLayout {
            id: goal
            required property var modelData
            objectName: "goal-" + modelData.goalId
            Layout.fillWidth: true
            spacing: 2
            Flow {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceSm
                SuiText { objectName: "goalTitle"; text: goal.modelData.title; variant: "subheading" }
                SuiChip { objectName: "goalStatus"; text: goal.modelData.status; tone: goal.modelData.tone }
                SuiText { objectName: "goalScope"; text: goal.modelData.scope; variant: "caption" }
                SuiButton {
                    objectName: "abandonGoal"
                    visible: goal.modelData.canAbandon
                    text: "Abandon"
                    variant: "secondary"
                    onClicked: practice.abandonGoal(goal.modelData.goalId)
                }
            }
            SuiText {
                objectName: "goalProgress"
                Layout.fillWidth: true
                text: goal.modelData.progress
                variant: "caption"
                wrapMode: Text.Wrap
            }
        }
    }
    SuiText {
        objectName: "goalsEmpty"
        Layout.fillWidth: true
        visible: practice.goals.length === 0
        text: "No goals yet — declare one below and it starts counting from your next take."
        variant: "caption"
        wrapMode: Text.Wrap
    }

    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: SuiTheme.line }

    Flow {
        objectName: "goalForm"
        Layout.fillWidth: true
        spacing: SuiTheme.spaceMd
        SuiSelect {
            id: metricSelect
            objectName: "goalMetric"
            width: 220
            label: "Metric"
            options: practice.metricOptions
            onValueSelected: (value) => practice.setField("metric", value)
        }
        SuiSelect {
            id: directionSelect
            objectName: "goalDirection"
            width: 140
            label: "Direction"
            options: practice.directionOptions
            onValueSelected: (value) => practice.setField("direction", value)
        }
        Column {
            spacing: 2
            SuiText { text: "Target"; variant: "label" }
            SuiTextField {
                objectName: "goalTarget"
                width: 110
                label: "Target"
                placeholderText: "5.5"
                inputMethodHints: Qt.ImhFormattedNumbersOnly
                text: practice.formTarget
                onTextChanged: if (text !== practice.formTarget) practice.setField("target", text)
            }
        }
        Column {
            spacing: 2
            SuiText { text: "Takes (N)"; variant: "label" }
            SuiTextField {
                objectName: "goalTakes"
                width: 90
                label: "Takes (N)"
                placeholderText: "10"
                inputMethodHints: Qt.ImhDigitsOnly
                text: practice.formTakes
                onTextChanged: if (text !== practice.formTakes) practice.setField("takesRequired", text)
            }
        }
        SuiSelect {
            id: scopeSelect
            objectName: "goalScopeSelect"
            width: 240
            label: "Scope"
            options: practice.scopeOptions
            onValueSelected: (value) => practice.setField("clusterId", value)
        }
        Column {
            spacing: 2
            SuiText { text: "Title (optional)"; variant: "label" }
            SuiTextField {
                objectName: "goalTitleField"
                width: 240
                label: "Title (optional)"
                placeholderText: "settle vibrato on Song C"
                text: practice.formTitle
                onTextChanged: if (text !== practice.formTitle) practice.setField("title", text)
            }
        }
        SuiButton {
            objectName: "preRegister"
            text: "Pre-register"
            variant: "primary"
            enabled: practice.canSubmit
            onClicked: practice.submitGoal()
        }
    }
}
