import React from "react";

// The fixed-position chart tooltip the legacy pages drove through a global div.
// A page mounts <ChartTooltipProvider> once; chart elements spread
// `tipHandlers(show, content)` and the provider renders the single tooltip node.
const ChartTooltipContext = React.createContext({ show: () => {}, hide: () => {} });

export function useChartTooltip() {
  return React.useContext(ChartTooltipContext);
}

// Convenience: mouse handlers for an SVG hit target. `content` may be a function
// (lazy) or a node; it renders inside the tooltip.
export function tipHandlers(tooltip, content) {
  return {
    onMouseMove: (event) => tooltip.show(event, typeof content === "function" ? content() : content),
    onMouseLeave: () => tooltip.hide()
  };
}

export function ChartTooltipProvider({ children }) {
  const [state, setState] = React.useState({ visible: false, x: 0, y: 0, content: null });
  const api = React.useMemo(() => ({
    show: (event, content) => setState({ visible: true, x: event.clientX, y: event.clientY, content }),
    hide: () => setState((prev) => (prev.visible ? { ...prev, visible: false } : prev))
  }), []);
  return (
    <ChartTooltipContext.Provider value={api}>
      {children}
      <div
        className={`vj-chart-tooltip${state.visible ? " vj-chart-tooltip-show" : ""}`}
        style={{ left: state.x, top: state.y }}
        role="status"
        aria-hidden={state.visible ? "false" : "true"}
      >
        {state.content}
      </div>
    </ChartTooltipContext.Provider>
  );
}
