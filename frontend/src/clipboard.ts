export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    // Plain HTTP may lack the secure-context clipboard API.
  }
  const previous = document.activeElement
  const input = document.createElement('textarea')
  input.value = value
  input.readOnly = true
  input.style.cssText = 'position:fixed;left:-9999px;top:0'
  document.body.append(input)
  input.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    input.remove()
    if (previous instanceof HTMLElement) previous.focus()
  }
}
