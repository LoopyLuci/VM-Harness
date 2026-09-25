use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyclass]
pub struct QemuSupervisor {
    name: String,
    state: std::sync::Mutex<String>,
}

#[pymethods]
impl QemuSupervisor {
    #[new]
    fn new(name: String) -> Self {
        Self {
            name,
            state: std::sync::Mutex::new("stopped".to_string()),
        }
    }

    fn start(&self) -> PyResult<String> {
        let mut s = self
            .state
            .lock()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        *s = "booting".to_string();
        *s = "running".to_string();
        Ok("started".to_string())
    }

    fn stop(&self) -> PyResult<String> {
        let mut s = self
            .state
            .lock()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        *s = "stopped".to_string();
        Ok("stopped".to_string())
    }

    fn state<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let state = self
            .state
            .lock()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        let dict = PyDict::new(py);
        dict.set_item("name", &self.name)?;
        dict.set_item("state", state.clone())?;
        Ok(dict)
    }
}

#[pymodule]
fn vmharness_supervisor(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QemuSupervisor>()?;
    Ok(())
}
