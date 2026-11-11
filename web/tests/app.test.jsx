import { test, expect } from 'vitest'
import React from 'react'
import { renderToString } from 'react-dom/server'
import App from '../src/App.jsx'

test('App renders', () => {
  expect(renderToString(React.createElement(App))).toContain('QMS demo')
})
