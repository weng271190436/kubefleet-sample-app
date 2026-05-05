import React, { useEffect, useState, useCallback } from 'react';
import { DataGrid } from '@mui/x-data-grid';
import {
  AppBar, Toolbar, Typography, Container, Button, Box, Snackbar, Alert, Chip,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import DeleteIcon from '@mui/icons-material/Delete';

const API_BASE = process.env.REACT_APP_API_URL || '/api';

const CATEGORY_COLORS = {
  limits: 'primary',
  fees: 'warning',
  security: 'error',
  rates: 'success',
  compliance: 'secondary',
  alerts: 'info',
  operations: 'default',
};

const columns = [
  { field: 'key', headerName: 'Key', flex: 1, editable: true },
  { field: 'value', headerName: 'Value', flex: 1, editable: true },
  {
    field: 'category',
    headerName: 'Category',
    width: 150,
    editable: true,
    type: 'singleSelect',
    valueOptions: ['limits', 'fees', 'security', 'rates', 'compliance', 'alerts', 'operations'],
    renderCell: (params) => (
      <Chip
        label={params.value}
        color={CATEGORY_COLORS[params.value] || 'default'}
        size="small"
        variant="outlined"
      />
    ),
  },
];

export default function App() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState([]);
  const [snackbar, setSnackbar] = useState(null);

  const fetchConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/configs`);
      const data = await res.json();
      setRows(data);
    } catch (err) {
      setSnackbar({ severity: 'error', message: 'Failed to load configs' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchConfigs(); }, [fetchConfigs]);

  const handleProcessRowUpdate = useCallback(async (newRow) => {
    try {
      const res = await fetch(`${API_BASE}/configs/${newRow.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: newRow.key,
          value: newRow.value,
          category: newRow.category,
        }),
      });
      if (!res.ok) throw new Error('Update failed');
      const updated = await res.json();
      setSnackbar({ severity: 'success', message: `Updated "${updated.key}"` });
      return updated;
    } catch (err) {
      setSnackbar({ severity: 'error', message: err.message });
      throw err;
    }
  }, []);

  const handleAddRow = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/configs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'new.config.key', value: '', category: 'limits' }),
      });
      if (!res.ok) throw new Error('Create failed');
      const created = await res.json();
      setRows((prev) => [...prev, created]);
      setSnackbar({ severity: 'success', message: 'Row added' });
    } catch (err) {
      setSnackbar({ severity: 'error', message: err.message });
    }
  }, []);

  const handleDeleteSelected = useCallback(async () => {
    try {
      await Promise.all(
        selectedIds.map((id) =>
          fetch(`${API_BASE}/configs/${id}`, { method: 'DELETE' })
        )
      );
      setRows((prev) => prev.filter((r) => !selectedIds.includes(r.id)));
      setSelectedIds([]);
      setSnackbar({ severity: 'success', message: `Deleted ${selectedIds.length} row(s)` });
    } catch (err) {
      setSnackbar({ severity: 'error', message: err.message });
    }
  }, [selectedIds]);

  return (
    <>
      <AppBar position="static">
        <Toolbar>
          <Typography variant="h6" sx={{ flexGrow: 1 }}>
            KubeFleet Sample App — Banking Configuration
          </Typography>
        </Toolbar>
      </AppBar>
      <Container maxWidth="lg" sx={{ mt: 4 }}>
        <Box display="flex" gap={1} mb={2}>
          <Button variant="contained" startIcon={<AddIcon />} onClick={handleAddRow}>
            Add Config
          </Button>
          <Button
            variant="outlined"
            color="error"
            startIcon={<DeleteIcon />}
            disabled={selectedIds.length === 0}
            onClick={handleDeleteSelected}
          >
            Delete Selected ({selectedIds.length})
          </Button>
        </Box>
        <Box sx={{ height: 500, width: '100%' }}>
          <DataGrid
            rows={rows}
            columns={columns}
            loading={loading}
            checkboxSelection
            disableRowSelectionOnClick
            processRowUpdate={handleProcessRowUpdate}
            onRowSelectionModelChange={setSelectedIds}
            rowSelectionModel={selectedIds}
            pageSizeOptions={[10, 25, 50]}
            initialState={{ pagination: { paginationModel: { pageSize: 10 } } }}
          />
        </Box>
      </Container>
      <Snackbar
        open={!!snackbar}
        autoHideDuration={3000}
        onClose={() => setSnackbar(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        {snackbar && (
          <Alert severity={snackbar.severity} onClose={() => setSnackbar(null)}>
            {snackbar.message}
          </Alert>
        )}
      </Snackbar>
    </>
  );
}
