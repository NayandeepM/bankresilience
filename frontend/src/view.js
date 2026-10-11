// One selected experiment owns all charts, diagnosis, events and verification.
export function experimentView(data, history, selectedId) {
  if (data.active) return data.experiment;
  if (selectedId) return history.find(h => h.id === selectedId)?.result ?? null;
  return data.experiment ?? data.last_result ?? null;
}
export function diagnosisFor(view) {
  const diagnosis = view?.diagnosis ?? view?.result?.diagnosis;
  return diagnosis && diagnosis.experiment_id === view.id && diagnosis.experiment_type === view.experiment_type ? diagnosis : null;
}
