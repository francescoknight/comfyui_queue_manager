import {EllipsisVertical, ArrowDownUp, Layers} from "lucide-react";
import {apiCall} from "../internals/functions";
import {useContext, useState, useRef, useEffect } from "react";
import {AppContext} from "../internals/app-context";

import AdsClickSharpIcon from '@mui/icons-material/AdsClickSharp';
import InfoOutlineSharpIcon from '@mui/icons-material/InfoOutlineSharp';
import QuizSharpIcon from '@mui/icons-material/QuizSharp';
import SwapVertIcon from '@mui/icons-material/SwapVert';

import {useAppStore} from "../stores/appStore";
import {useOptionsStore} from "../stores/optionsStore";

export default function TopMenu() {
  const [uiState, setUiState] = useState({
    menuOpen: false,
  });

  const {openSplash} = useContext(AppContext)

  const submitOrder = useOptionsStore((state) => state.submit_order);
  const setDirectOption = useOptionsStore((state) => state.setDirectOption);
  const [orderInfo, setOrderInfo] = useState(null);

  async function toggleSubmitOrder() {
    const next = submitOrder === "LIFO" ? "FIFO" : "LIFO";
    setDirectOption("submit_order", next);
    try {
      await apiCall("queue_manager/options", {key: "submit_order", value: next}, "POST");
      setOrderInfo(next === "LIFO"
        ? "New jobs will now jump to the front of the queue."
        : "New jobs will now be appended at the end of the queue.");
    } catch (e) {
      setDirectOption("submit_order", submitOrder);
      setOrderInfo("Could not change the submit order.");
    }
  }

  async function optimizeOrder() {
    setOrderInfo("Grouping jobs by model / LoRA...");
    try {
      const result = await apiCall("queue_manager/optimize", {}, "POST");
      if (!result || !result.total) {
        setOrderInfo("Nothing to group.");
      } else if (!result.reordered) {
        setOrderInfo(`Already grouped: ${result.total} job(s) in ${result.groups} group(s).`);
      } else {
        setOrderInfo(`${result.total} job(s) in ${result.groups} group(s) - model loads ${result.model_loads_before} -> ${result.model_loads_after}.`);
      }
    } catch (e) {
      setOrderInfo("Grouping failed, see the console.");
    }
  }

  async function invertQueue() {
    setOrderInfo("Inverting the queue...");
    try {
      const result = await apiCall("queue_manager/invert", {}, "POST");
      setOrderInfo(result?.moved ? `Queue inverted: ${result.moved} job(s).` : "Nothing to invert.");
    } catch (e) {
      setOrderInfo("Invert failed.");
    }
  }

  const menuRef = useRef(null);
  const toggleRef = useRef(null);

  function toggleMenu() {
    setUiState(prev => ({...prev, menuOpen: !prev.menuOpen}));
  }

  async function takeOver() {
    toggleMenu();
    apiCall('queue_manager/takeover?client_id='+useAppStore.getState().clientId, null, "GET");
  }

  useEffect(() => {
    if (!uiState.menuOpen) return;

    function onPointerDown(e) {
      const menuEl = menuRef.current;
      const toggleEl = toggleRef.current;
      const target = e.target;

      if (!menuEl || !toggleEl) return;
      if (menuEl.contains(target)) return;
      if (toggleEl.contains(target)) return;

      setUiState(prev => ({...prev, menuOpen: false}));
    }

    document.addEventListener("pointerdown", onPointerDown, {capture: true});
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, {capture: true});
    };
  }, [uiState.menuOpen]);

  return (
    <>
      <button
        ref={toggleRef}
        className={'top-menu-toggle' + (uiState.menuOpen ? ' open' : '')} onClick={toggleMenu}>
        <span>
          <EllipsisVertical/>
        </span>
      </button>
      <section
        ref={menuRef}
        className={"top-menu" + (uiState.menuOpen ? ' open' : '')}>
        <div className={"container"}>
          <button type={"button"} className={"button shiny-button"} onClick={toggleSubmitOrder}
                  title={"Where a newly queued job lands. FIFO: at the end of the queue. LIFO: at the front, so it runs next."}>
            <ArrowDownUp /> New jobs: {submitOrder === "LIFO" ? "LIFO (run first)" : "FIFO (run last)"}
          </button>
          <button type={"button"} className={"button shiny-button"} onClick={optimizeOrder}
                  title={"Reorder the pending queue so jobs sharing the same models / LoRAs run back to back"}>
            <Layers /> Group by model / LoRA
          </button>
          <button type={"button"} className={"button shiny-button"} onClick={invertQueue}
                  title={"Reverse the pending queue: the last job becomes the first"}>
            <SwapVertIcon /> Invert queue order
          </button>
          {orderInfo ? (
            <div style={{fontSize: "0.75rem", opacity: 0.8, padding: "0.25rem 0.5rem", lineHeight: 1.3}}>
              {orderInfo}
            </div>
          ) : null}
          <button type={"button"}  className={"button shiny-button"} onClick={takeOver}><AdsClickSharpIcon /> Take over focus</button>
          <a className={"button shiny-button"} href={"https://github.com/QuietNoise/comfyui_queue_manager?tab=readme-ov-file#manual"} target={"_blank"}><QuizSharpIcon /> Documentation</a>
          <button type={"button"}  className={"button shiny-button"} onClick={() => {toggleMenu(); openSplash()}}><InfoOutlineSharpIcon /> About Queue Manager</button>
        </div>
      </section>
    </>
  );
}
