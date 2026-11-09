import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Bot, Check, ChevronDown, LoaderCircle, MessageSquarePlus, Pencil, RefreshCw, Send, Settings2, Square, Trash2, X } from 'lucide-react'
import { api } from './api'
import type { AskConversation, AskMessage, AskStatus } from './api'
import type { WorkspaceContextSnapshot, WorkspaceContextTab } from './askDexter/context'

type Props = { tabs: WorkspaceContextTab[]; activeTabId: string | null }
type ContextMode = 'active' | 'pinned' | 'selected_tabs'
type StreamMessage = { type: string; [key: string]: unknown }

function messageText(text: string) {
  const parts: Array<{ type: 'text' | 'code'; value: string }> = []
  const lines = text.split('\n')
  let code: string[] | null = null
  for (const line of lines) {
    if (line.startsWith('```')) {
      if (code) { parts.push({ type: 'code', value: code.join('\n') }); code = null }
      else code = []
    } else if (code) code.push(line)
    else parts.push({ type: 'text', value: line })
  }
  if (code) parts.push({ type: 'code', value: code.join('\n') })
  return parts.map((part, index) => part.type === 'code'
    ? <pre className="ask-code-block" key={index}><code>{part.value}</code></pre>
    : <p key={index}>{part.value.split(/(`[^`]+`)/g).map((segment, partIndex) => segment.startsWith('`') && segment.endsWith('`')
      ? <code key={partIndex}>{segment.slice(1, -1)}</code> : segment)}</p>)
}

function contextLabel(tab?: WorkspaceContextTab | null) {
  if (!tab) return 'No workspace tab is open'
  return `${tab.title}${tab.environment ? ` · ${tab.environment.toUpperCase()}` : ''}`
}

export function AskDexterPanel({ tabs, activeTabId }: Props) {
  const [status, setStatus] = useState<AskStatus | null>(null)
  const [conversations, setConversations] = useState<AskConversation[]>([])
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [loadedConversationId, setLoadedConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<AskMessage[]>([])
  const [contextMode, setContextMode] = useState<ContextMode>('active')
  const [selectedTabIds, setSelectedTabIds] = useState<string[]>([])
  const [pinnedContext, setPinnedContext] = useState<WorkspaceContextTab | null>(null)
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [streamingText, setStreamingText] = useState('')
  const [streamState, setStreamState] = useState('')
  const [error, setError] = useState('')
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false)
  const [contextMenuOpen, setContextMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [endpointDraft, setEndpointDraft] = useState('')
  const [agentDraft, setAgentDraft] = useState('build')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [modelVariant, setModelVariant] = useState('')
  const messagesEnd = useRef<HTMLDivElement>(null)
  const streamRef = useRef<EventSource | null>(null)
  const selectedConversation = conversations.find((item) => item.id === activeConversationId) ?? null
  const activeContext = tabs.find((item) => item.tab_id === activeTabId) ?? null
  const chosenModel = selectedConversation?.model
    ? status?.models.find((item) => item.provider_id === selectedConversation.model?.provider_id && item.id === selectedConversation.model?.id)
      ? { ...status.models.find((item) => item.provider_id === selectedConversation.model?.provider_id && item.id === selectedConversation.model?.id)!, ...selectedConversation.model }
      : selectedConversation.model
    : status?.default_model ?? null
  const modelVariantChoices = chosenModel?.variants?.filter((item) => item !== 'none') ?? []
  const pinnedAvailable = Boolean(pinnedContext && tabs.some((tab) => tab.tab_id === pinnedContext.tab_id))
  const selectedAvailableCount = selectedTabIds.filter((id) => tabs.some((tab) => tab.tab_id === id)).length

  const closeStream = useCallback(() => {
    streamRef.current?.close()
    streamRef.current = null
  }, [])

  const attachStream = useCallback((conversationId: string, requestId: string) => {
    closeStream()
    const source = new EventSource(api.askStreamUrl(conversationId, requestId))
    streamRef.current = source
    setStreamState('Connecting')
    source.onmessage = (event) => {
      let payload: StreamMessage
      try { payload = JSON.parse(event.data) as StreamMessage } catch { return }
      if (payload.type === 'connected') { setStreamState('Generating'); setError('') }
      else if (payload.type === 'generating') setStreamState('Generating')
      else if (payload.type === 'delta' && typeof payload.delta === 'string') {
        setStreamingText((value) => value + payload.delta)
        setStreamState('Generating')
      } else if (payload.type === 'replace' && typeof payload.text === 'string') {
        setStreamingText(payload.text)
        setStreamState('Generating')
      } else if (payload.type === 'done') {
        closeStream(); setBusy(false); setStreamState('')
        setStreamingText('')
        if (payload.state !== 'completed') setError(payload.state === 'cancelled' ? 'The response was stopped.' : 'The response ended before OpenCode marked it complete. Your conversation history has been recovered.')
        void api.askMessages(conversationId).then((result) => setMessages(result.items)).catch(() => undefined)
        void api.askConversations().then((result) => setConversations(result.items)).catch(() => undefined)
      } else if (payload.type === 'error' || payload.type === 'reconnect') {
        closeStream(); setBusy(false); setStreamState('')
        setError(typeof payload.message === 'string' ? payload.message : 'The response was interrupted. Reopen the conversation to reconnect.')
        void api.askMessages(conversationId).then((result) => setMessages(result.items)).catch(() => undefined)
      }
    }
    source.onerror = () => setStreamState('Reconnecting')
  }, [closeStream])

  const loadConversation = useCallback(async (conversationId: string | null) => {
    closeStream()
    setMessages([]); setStreamingText(''); setBusy(false); setError('')
    setLoadedConversationId(conversationId)
    setActiveConversationId(conversationId)
    if (!conversationId) return
    try {
      await api.askActivateConversation(conversationId)
      const conversation = conversations.find((item) => item.id === conversationId)
      if (conversation) {
        const preferences = conversation.context ?? {}
        setContextMode(preferences.mode ?? conversation.context_mode ?? 'active')
        setSelectedTabIds(preferences.selected_tab_ids ?? [])
        setPinnedContext(preferences.pinned_context ?? null)
        setModelVariant(conversation.model?.variant ?? '')
      }
      const result = await api.askMessages(conversationId)
      setMessages(result.items)
      if (result.active_request?.request_id) {
        setBusy(true)
        attachStream(conversationId, result.active_request.request_id)
      }
      setLoadedConversationId(conversationId)
    } catch (cause) { setLoadedConversationId(null); setError(cause instanceof Error ? cause.message : 'This conversation could not be loaded.') }
  }, [attachStream, closeStream, conversations])

  const refresh = useCallback(async (initial = false) => {
    if (initial) setLoading(true)
    const [connection, saved] = await Promise.allSettled([api.askStatus(), api.askConversations()])
    if (connection.status === 'fulfilled') {
      setStatus(connection.value)
      setEndpointDraft(connection.value.endpoint ?? '')
      setAgentDraft(connection.value.agent_id ?? connection.value.agent?.id ?? 'build')
    } else setError(connection.reason instanceof Error ? connection.reason.message : 'OpenCode is unavailable.')
    if (saved.status === 'fulfilled') {
      setConversations(saved.value.items)
      const selected = saved.value.active_conversation_id && saved.value.items.some((item) => item.id === saved.value.active_conversation_id)
        ? saved.value.active_conversation_id : saved.value.items[0]?.id ?? null
      if (selected !== activeConversationId) setActiveConversationId(selected)
    }
    setLoading(false)
  }, [activeConversationId])

  useEffect(() => { void refresh(true) }, [])
  useEffect(() => {
    if (!activeConversationId || loading || loadedConversationId === activeConversationId || !selectedConversation) return
    void loadConversation(selectedConversation.id)
  }, [activeConversationId, conversations, loading, loadedConversationId, loadConversation, selectedConversation])
  useEffect(() => () => closeStream(), [closeStream])
  useEffect(() => { messagesEnd.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [messages, streamingText, busy])

  const createConversation = async () => {
    setBusy(true); setError('')
    try {
      const conversation = await api.askCreateConversation()
      setConversations((items) => [conversation, ...items])
      setActiveConversationId(conversation.id)
      setLoadedConversationId(conversation.id)
      setContextMode('active'); setSelectedTabIds([]); setPinnedContext(null)
      setMessages([]); setModelVariant(conversation.model?.variant ?? '')
      await api.askActivateConversation(conversation.id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'A new conversation could not be created.') }
    finally { setBusy(false) }
  }

  const updatePreferences = async (mode: ContextMode, selected = selectedTabIds, pinned = pinnedContext) => {
    if (!selectedConversation) return
    const pinnedValue = mode === 'pinned' ? (pinned === undefined ? activeContext : pinned) : pinned
    setContextMode(mode); setSelectedTabIds(selected); setPinnedContext(pinnedValue ?? null)
    try {
      const saved = await api.askSetContext(selectedConversation.id, {
        mode, selected_tab_ids: selected, pinned_context: pinnedValue ?? null,
      })
      setConversations((items) => items.map((item) => item.id === saved.id ? saved : item))
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Workspace context settings could not be saved.') }
  }

  const chooseModel = async (modelKey: string) => {
    if (!selectedConversation || !status) return
    const [providerId, modelId] = modelKey.split('\u0000')
    const model = status.models.find((item) => item.provider_id === providerId && item.id === modelId)
    if (!model) return
    try {
      const saved = await api.askSetModel(selectedConversation.id, { provider_id: model.provider_id, id: model.id, variant: undefined })
      setConversations((items) => items.map((item) => item.id === saved.id ? saved : item))
      setModelVariant('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The selected model could not be saved.') }
  }

  const chooseVariant = async (variant: string) => {
    if (!selectedConversation?.model) return
    try {
      const saved = await api.askSetModel(selectedConversation.id, { ...selectedConversation.model, variant: variant || undefined })
      setConversations((items) => items.map((item) => item.id === saved.id ? saved : item))
      setModelVariant(variant)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The model variant could not be saved.') }
  }

  const submit = async () => {
    if (!draft.trim() || busy || !status?.connected || !selectedConversation) return
    if (!selectedConversation.model) { setError('Choose an OpenCode model before sending a message.'); return }
    const text = draft.trim()
    const requestId = crypto.randomUUID()
    const snapshot: WorkspaceContextSnapshot = { version: 1, mode: contextMode, active_tab_id: activeTabId, tabs }
    setDraft(''); setError(''); setBusy(true); setStreamingText(''); setStreamState('Sending')
    const optimistic: AskMessage = { id: `pending-${requestId}`, role: 'user', text, created_at: Date.now() }
    setMessages((items) => [...items, optimistic])
    try {
      await api.askSend(selectedConversation.id, requestId, text, snapshot)
      attachStream(selectedConversation.id, requestId)
      const list = await api.askConversations()
      setConversations(list.items)
    } catch (cause) {
      setMessages((items) => items.filter((item) => item.id !== optimistic.id))
      setBusy(false); setStreamState('')
      setError(cause instanceof Error ? cause.message : 'The message could not be sent.')
    }
  }

  const stop = async () => {
    if (!selectedConversation || !busy) return
    try { await api.askStop(selectedConversation.id) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'The response could not be stopped.') }
    finally {
      closeStream(); setBusy(false); setStreamState(''); setStreamingText('')
      void api.askMessages(selectedConversation.id).then((result) => setMessages(result.items)).catch(() => undefined)
    }
  }

  const rename = async () => {
    if (!selectedConversation || !renameValue.trim()) return
    try {
      const saved = await api.askRenameConversation(selectedConversation.id, renameValue.trim())
      setConversations((items) => items.map((item) => item.id === saved.id ? saved : item))
      setRenaming(false)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The conversation could not be renamed.') }
  }

  const removeConversation = async () => {
    if (!selectedConversation) return
    if (!window.confirm(`Delete “${selectedConversation.title}” and its OpenCode conversation history?`)) return
    try {
      await api.askDeleteConversation(selectedConversation.id)
      const remaining = conversations.filter((item) => item.id !== selectedConversation.id)
      setConversations(remaining)
      await loadConversation(remaining[0]?.id ?? null)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The conversation could not be deleted.') }
  }

  const saveEndpoint = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const saved = await api.askConfigureEndpoint(endpointDraft.trim() || null, agentDraft.trim())
      setStatus(saved); setEndpointDraft(saved.endpoint ?? ''); setAgentDraft(saved.agent_id ?? saved.agent?.id ?? agentDraft); setError('')
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'The OpenCode endpoint could not be saved.') }
  }

  const modelValue = chosenModel ? `${chosenModel.provider_id}\u0000${chosenModel.id}` : ''
  const contextSummary = contextMode === 'active' ? contextLabel(activeContext)
    : contextMode === 'pinned' ? `${contextLabel(pinnedContext)} · ${pinnedAvailable ? 'pinned' : 'unavailable'}`
      : `${selectedAvailableCount} of ${selectedTabIds.length} selected tabs available`

  return <div className="ask-panel-body">
    <div className="ask-toolbar">
      <div className="ask-status"><span className={`ask-status-dot ${status?.connected ? 'online' : 'offline'}`} /><span>{status?.connected ? 'Connected' : loading ? 'Connecting' : 'OpenCode unavailable'}</span></div>
      <button className="ask-toolbar-button" aria-label="Reconnect OpenCode" title="Reconnect OpenCode" onClick={() => { setLoading(true); void api.askReconnect().then(setStatus).catch((cause) => setError(cause instanceof Error ? cause.message : 'OpenCode reconnection failed.')).finally(() => setLoading(false)) }}><RefreshCw size={13} /></button>
      <button className="ask-toolbar-button" aria-label="OpenCode settings" title="OpenCode settings" aria-expanded={settingsOpen} onClick={() => setSettingsOpen((value) => !value)}><Settings2 size={14} /></button>
    </div>
    {settingsOpen && <form className="ask-runtime-settings" onSubmit={(event) => void saveEndpoint(event)}>
      <label><span>OpenCode agent</span><select value={agentDraft} onChange={(event) => setAgentDraft(event.target.value)}>
        {!status?.agents.some((item) => item.id === agentDraft) && <option value={agentDraft}>{agentDraft} · unavailable</option>}
        {(status?.agents ?? []).map((item) => <option key={item.id} value={item.id}>{item.id}{item.id === 'build' ? ' · restricted assistant' : ''}</option>)}
      </select></label>
      <label><span>Server endpoint</span><input value={endpointDraft} onChange={(event) => setEndpointDraft(event.target.value)} aria-label="OpenCode server endpoint" placeholder="Automatic local discovery" /></label>
      <div className="ask-runtime-settings-footer"><small>Local HTTP only. Remote OpenCode servers must use HTTPS.</small><button>Save and reconnect</button></div>
    </form>}

    <div className="ask-conversation-bar">
      {renaming && selectedConversation ? <form className="ask-rename-form" onSubmit={(event) => { event.preventDefault(); void rename() }}><input aria-label="Conversation title" value={renameValue} onChange={(event) => setRenameValue(event.target.value)} autoFocus /><button aria-label="Save conversation title"><Check size={14} /></button><button type="button" aria-label="Cancel rename" onClick={() => setRenaming(false)}><X size={14} /></button></form> : <button className="ask-conversation-select" onClick={() => setConversationMenuOpen((value) => !value)} aria-expanded={conversationMenuOpen}><span>{selectedConversation?.title ?? 'Choose a conversation'}</span><ChevronDown size={14} /></button>}
      <button className="ask-small-action" aria-label="New chat" title="New chat" disabled={!status?.connected || busy} onClick={() => void createConversation()}><MessageSquarePlus size={14} /></button>
      {selectedConversation && <button className="ask-small-action" aria-label="Rename conversation" title="Rename conversation" onClick={() => { setRenameValue(selectedConversation.title); setRenaming(true) }}><Pencil size={13} /></button>}
      {selectedConversation && <button className="ask-small-action danger" aria-label="Delete conversation" title="Delete conversation" onClick={() => void removeConversation()}><Trash2 size={13} /></button>}
    </div>
    {conversationMenuOpen && <div className="ask-conversation-list" role="listbox" aria-label="Conversations">
      {!conversations.length && <span className="ask-muted">No saved conversations yet.</span>}
      {conversations.map((item) => <button key={item.id} role="option" aria-selected={item.id === activeConversationId} onClick={() => { setConversationMenuOpen(false); void loadConversation(item.id) }}><span>{item.title}</span><small>{new Date(item.last_activity_at).toLocaleDateString()}</small></button>)}
    </div>}

    <div className="ask-model-row">
      <label className="ask-model-select"><span>Model</span><select aria-label="OpenCode model" value={modelValue} disabled={!selectedConversation || !status?.models.length || busy} onChange={(event) => void chooseModel(event.target.value)}>
        <option value="">{status?.models.length ? 'Choose a model' : 'No connected models'}</option>
        {(status?.models ?? []).map((item) => <option key={`${item.provider_id}/${item.id}`} value={`${item.provider_id}\u0000${item.id}`}>{item.name} · {item.provider_name}</option>)}
      </select></label>
      {modelVariantChoices.length > 0 && <label className="ask-variant-select"><span>Reasoning</span><select aria-label="Model reasoning variant" disabled={!selectedConversation || busy} value={modelVariant} onChange={(event) => void chooseVariant(event.target.value)}><option value="">Default</option>{modelVariantChoices.map((variant) => <option key={variant} value={variant}>{variant}</option>)}</select></label>}
    </div>

    <div className="ask-context-box">
      <div className="ask-context-title"><span>Workspace context</span><button aria-expanded={contextMenuOpen} onClick={() => setContextMenuOpen((value) => !value)}><Settings2 size={13} /> {contextMode === 'active' ? 'Follow active tab' : contextMode === 'pinned' ? 'Pinned' : 'Selected tabs'}</button></div>
      <div className="ask-context-summary" title={contextSummary}>{contextMode === 'active' && activeContext ? <><span className="ask-context-mark" />{contextLabel(activeContext)}</> : contextMode === 'pinned' && pinnedContext ? <><span className={`ask-context-mark ${pinnedAvailable ? 'pinned' : 'unavailable'}`} />{contextLabel(pinnedContext)}{!pinnedAvailable && <span className="ask-context-unavailable">tab closed</span>}</> : contextMode === 'selected_tabs' ? <><span className="ask-context-mark selected" />{contextSummary}</> : <><span className="ask-context-mark empty" />No tab context</>}</div>
      {contextMenuOpen && <div className="ask-context-menu">
        <label><input type="radio" name="ask-context" checked={contextMode === 'active'} onChange={() => void updatePreferences('active')} /><span><strong>Follow active tab</strong><small>Use the currently active Dexter workspace tab for each message.</small></span></label>
        <label><input type="radio" name="ask-context" checked={contextMode === 'pinned'} onChange={() => void updatePreferences('pinned')} /><span><strong>Pin this context</strong><small>{pinnedContext ? `${contextLabel(pinnedContext)}${pinnedAvailable ? '' : ' · unavailable because the tab is closed'}` : activeContext ? `Pin ${contextLabel(activeContext)}` : 'Open a workspace tab to pin its context.'}</small></span></label>
        <label><input type="radio" name="ask-context" checked={contextMode === 'selected_tabs'} onChange={() => void updatePreferences('selected_tabs')} /><span><strong>Select tabs</strong><small>Choose up to five workspace tabs.</small></span></label>
        {contextMode === 'selected_tabs' && <div className="ask-tab-choices">{tabs.length ? tabs.map((tab) => <label key={tab.tab_id}><input type="checkbox" checked={selectedTabIds.includes(tab.tab_id)} disabled={!selectedTabIds.includes(tab.tab_id) && selectedTabIds.length >= 5} onChange={(event) => { const next = event.target.checked ? [...selectedTabIds, tab.tab_id] : selectedTabIds.filter((id) => id !== tab.tab_id); void updatePreferences('selected_tabs', next) }} /><span>{contextLabel(tab)}</span>{tab.tab_id === activeTabId && <small>Active</small>}</label>) : <span className="ask-muted">Open a workspace tab to select it.</span>}</div>}
        {contextMode === 'pinned' && pinnedContext && <button className="ask-clear-pin" onClick={() => { setPinnedContext(null); void updatePreferences('pinned', selectedTabIds, null) }}>Clear pinned context</button>}
      </div>}
    </div>

    <div className="ask-messages" aria-live="polite">
      {loading ? <div className="ask-empty"><LoaderCircle size={18} className="spin" /><span>Connecting to OpenCode…</span></div>
          : !status?.connected ? <div className="ask-empty"><Bot size={22} /><strong>OpenCode is unavailable</strong><span>{status?.message ?? error ?? 'Check that OpenCode is installed and authenticated.'}</span><button onClick={() => { setLoading(true); void api.askReconnect().then((value) => { setStatus(value); setAgentDraft(value.agent_id ?? value.agent?.id ?? 'build'); setError('') }).catch((cause) => setError(cause instanceof Error ? cause.message : 'Retry failed.')).finally(() => setLoading(false)) }}>Retry connection</button><small>Check the selected agent and local OpenCode authentication in settings.</small></div>
          : !selectedConversation ? <div className="ask-empty"><Bot size={23} /><strong>Start a conversation</strong><span>Ask about the active workspace or select tabs as context.</span><button disabled={!status.models.length} onClick={() => void createConversation()}>New chat</button>{!status.preferred_model_available && <small>Luna 6 is not connected. Choose an available model after creating a chat.</small>}</div>
            : messages.length === 0 && !busy ? <div className="ask-empty"><Bot size={23} /><strong>Ask Dexter</strong><span>{contextMode === 'active' && activeContext ? `Ready to discuss ${contextLabel(activeContext)}.` : 'Your workspace context is attached when you send a message.'}</span></div>
              : <>{messages.map((item) => <article key={item.id} className={`ask-message ${item.role}`}><div className="ask-message-role">{item.role === 'user' ? 'You' : 'Ask Dexter'}</div><div className="ask-message-content">{messageText(item.text)}</div></article>)}{busy && <article className="ask-message assistant streaming"><div className="ask-message-role">Ask Dexter <span>{streamState || 'Thinking'}</span></div><div className="ask-message-content">{streamingText ? messageText(streamingText) : <span className="ask-thinking"><LoaderCircle size={13} className="spin" /> Thinking…</span>}</div></article>}<div ref={messagesEnd} /></>}
    </div>

    {error && <div className="ask-error" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError('')}><X size={12} /></button></div>}
    <form className="ask-composer" onSubmit={(event) => { event.preventDefault(); void submit() }}>
      <textarea value={draft} aria-label="Ask a question" placeholder={status?.connected ? contextMode === 'active' && activeContext ? `Ask about ${activeContext.title}…` : 'Ask a question about your workspace…' : 'OpenCode is unavailable'} disabled={!status?.connected || !selectedConversation || busy} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} rows={2} />
      <div className="ask-composer-footer"><span>Enter to send · Shift+Enter for a new line</span>{busy ? <button type="button" aria-label="Stop response" title="Stop response" className="ask-stop-button" onClick={() => void stop()}><Square size={13} /></button> : <button aria-label="Send message" title="Send message" disabled={!draft.trim() || !status?.connected || !selectedConversation?.model}><Send size={15} /></button>}</div>
    </form>
    {status?.connected && !status.preferred_model_available && <div className="ask-footnote">Preferred model Luna 6 is not connected. Select another model if needed.</div>}
  </div>
}
